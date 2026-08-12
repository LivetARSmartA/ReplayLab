from __future__ import annotations
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any
from .diagnostics import get_logger, open_native_stderr
from .native_runtime import native_binary_candidates
HOST_PROTOCOL = 'replaylab-runtime-v1'
LOGGER = get_logger('runtime')
HOST_STATUS = {1: 'Runtime Engine received an invalid request', 2: 'Runtime Engine could not read the requested file', 3: 'This Warcraft build is unsupported', 4: 'Runtime Engine could not open Warcraft', 5: 'Runtime Engine could not read Warcraft memory'}

class NativeRuntimeError(RuntimeError):

    def __init__(self, status: int, detail: int, message: str, response: dict[str, Any] | None=None) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail
        self.response = response or {}

def find_native_runtime_host() -> Path:
    for candidate in native_binary_candidates('replaylab_runtime_host.exe', environment_variable='REPLAYLAB_RUNTIME_HOST', build_subdirectory='runtime'):
        if candidate.is_file():
            return candidate.resolve()
    raise NativeRuntimeError(2, 0, 'Runtime Engine native component was not found. Reinstall ReplayLab.')

class NativeRuntimeHost:

    def __init__(self, *, executable: Path | None=None) -> None:
        if os.name != 'nt':
            raise NativeRuntimeError(4, 0, 'Runtime Engine requires Windows')
        host = executable.resolve() if executable else find_native_runtime_host()
        self._stderr_log = open_native_stderr('runtime')
        try:
            self._process: subprocess.Popen[str] = subprocess.Popen([str(host)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr_log, text=True, encoding='utf-8', errors='strict', bufsize=1, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except Exception:
            self._stderr_log.close()
            LOGGER.exception('Could not start Runtime Engine: %s', host)
            raise
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._request_id = 0
        self._closed = False

    def exchange(self, command: str, fields: dict[str, object] | None=None) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise NativeRuntimeError(4, 0, 'Runtime Engine is closed')
            stdin = self._process.stdin
            stdout = self._process.stdout
            if stdin is None or stdout is None:
                raise NativeRuntimeError(4, 0, 'Runtime Engine IPC is unavailable')
            self._request_id += 1
            request_id = self._request_id
            request = {'protocol': HOST_PROTOCOL, 'request_id': request_id, 'command': command, **(fields or {})}
            try:
                stdin.write(json.dumps(request, ensure_ascii=False, separators=(',', ':')) + '\n')
                stdin.flush()
                line = stdout.readline()
                if not line:
                    raise NativeRuntimeError(4, 0, 'Runtime Engine terminated unexpectedly')
                response = json.loads(line)
            except (BrokenPipeError, OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise NativeRuntimeError(4, 0, f'Runtime Engine IPC failed: {exc}') from exc
            if not isinstance(response, dict) or response.get('protocol') != HOST_PROTOCOL or response.get('request_id') != request_id or (response.get('command') != command):
                raise NativeRuntimeError(1, 0, 'Runtime Engine protocol mismatch')
            status = int(response.get('status', 1))
            detail = int(response.get('detail', 0))
            if status:
                message = str(response.get('error') or HOST_STATUS.get(status, f'Runtime Engine error {status}'))
                if detail:
                    message += f' (WinError {detail})'
                raise NativeRuntimeError(status, detail, message, response)
            return response

    def close(self) -> None:
        with self._close_lock:
            process = getattr(self, '_process', None)
            if process is None or self._closed:
                return
            if process.poll() is None:
                try:
                    self.exchange('shutdown')
                except (NativeRuntimeError, OSError):
                    pass
            self._closed = True
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1.0)
            self._stderr_log.close()

    def __enter__(self) -> NativeRuntimeHost:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

def native_file_sha256(path: str | Path) -> str:
    with NativeRuntimeHost() as host:
        response = host.exchange('sha256', {'path': str(Path(path).resolve())})
    digest = str(response.get('sha256', ''))
    if len(digest) != 64:
        raise NativeRuntimeError(2, 0, 'Runtime Engine returned an invalid digest')
    return digest
