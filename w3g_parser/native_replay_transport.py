from __future__ import annotations
import ctypes
import json
import os
import subprocess
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable
from .diagnostics import get_logger, open_native_stderr
from .native_runtime import native_binary_candidates
from .seeker import SeekBackendError, SeekCancelled, SeekMetrics, SeekProfile, SeekProgress
HOST_PROTOCOL = 'replaylab-replay-transport-v2'
LOGGER = get_logger('replay_transport')
HOST_STATUS = {2: 'Native Seeker rejected the request', 3: 'Warcraft replay state is no longer valid', 4: 'Native Seeker could not read Warcraft state', 5: 'Native Seeker could not update Warcraft playback', 6: 'Native Seeker timed out', 7: 'Native Seeker progress channel failed', 8: 'Native Seeker could not restore paused playback', 100: 'Native Seeker received an invalid protocol message', 101: 'Native Seeker is not configured', 102: 'Native Seeker could not open Warcraft', 103: 'Native Seeker could not open its cancellation channel'}

def _native_host_candidates() -> list[Path]:
    return native_binary_candidates('replaylab_replay_transport_host.exe', environment_variable='REPLAYLAB_REPLAY_TRANSPORT_HOST', build_subdirectory='replay_transport')

def find_native_replay_transport_host() -> Path:
    for candidate in _native_host_candidates():
        if candidate.is_file():
            return candidate.resolve()
    raise SeekBackendError('Native Seeker component was not found. Reinstall ReplayLab.')

class NativeReplayTransport:

    @property
    def pid(self) -> int:
        return self.process_id

    def __init__(self, process_id: int | None=None, *, executable: Path | None=None) -> None:
        if os.name != 'nt':
            raise SeekBackendError('Native Seeker requires Windows')
        if process_id is not None and process_id <= 0:
            raise ValueError('Native Seeker session is invalid')
        requested_process_id = process_id or 0
        self.process_id = requested_process_id
        self.replay_block = 0
        self._process: subprocess.Popen[str] | None = None
        self._stderr_log = None
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._request_id = 0
        self._closed = False
        self._kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self._kernel32.CreateEventW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)
        self._kernel32.CreateEventW.restype = wintypes.HANDLE
        self._kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
        self._kernel32.SetEvent.restype = wintypes.BOOL
        self._kernel32.ResetEvent.argtypes = (wintypes.HANDLE,)
        self._kernel32.ResetEvent.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._event_name = f'Local\\ReplayLab-Transport-{requested_process_id}-{uuid.uuid4().hex}'
        self._cancel_handle = self._kernel32.CreateEventW(None, True, False, self._event_name)
        if not self._cancel_handle:
            error = ctypes.get_last_error()
            raise SeekBackendError(f'Could not create Native Seeker cancellation channel (WinError {error})')
        try:
            host = executable.resolve() if executable else find_native_replay_transport_host()
            creation_flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            self._stderr_log = open_native_stderr('replay_transport')
            self._process = subprocess.Popen([str(host)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr_log, text=True, encoding='utf-8', errors='strict', bufsize=1, creationflags=creation_flags)
            event = self._exchange_once('configure', {'process_id': requested_process_id, 'cancel_event': self._event_name})
            self.process_id = int(event['process_id'])
            self.replay_block = int(event['replay_block'])
            self.replay_position_ms = int(event['current_replay_time_ms'])
            self.replay_length_ms = int(event['replay_length_ms'])
        except Exception:
            self.close()
            raise
        LOGGER.info('Replay transport configured: pid=%s block=0x%X host=%s', self.process_id, self.replay_block, host)

    @staticmethod
    def _optional(value: object) -> float | None:
        return None if value is None else float(value)

    @staticmethod
    def _raise_status(event: dict[str, Any]) -> None:
        status = int(event.get('status', 0))
        if status == 0:
            return
        message = str(event.get('message', '')).strip() or HOST_STATUS.get(status, f'Native Seeker error {status}')
        detail = int(event.get('detail', 0))
        suffix = f' (WinError {detail})' if detail else ''
        if status == 1:
            raise SeekCancelled(message)
        raise SeekBackendError(f'{message}{suffix}')

    def _write_request(self, command: str, fields: dict[str, object] | None=None) -> int:
        process = self._process
        if self._closed or process is None or process.stdin is None:
            raise SeekBackendError('Native Seeker is closed')
        self._request_id += 1
        request_id = self._request_id
        request = {'protocol': HOST_PROTOCOL, 'request_id': request_id, 'command': command, **(fields or {})}
        try:
            process.stdin.write(json.dumps(request, ensure_ascii=False, separators=(',', ':')) + '\n')
            process.stdin.flush()
        except (BrokenPipeError, OSError, UnicodeError) as exc:
            raise SeekBackendError(f'Native Seeker IPC write failed: {exc}') from exc
        return request_id

    def _read_event(self, command: str, request_id: int) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdout is None:
            raise SeekBackendError('Native Seeker output is unavailable')
        try:
            line = process.stdout.readline()
            if not line:
                raise SeekBackendError('Native Seeker terminated unexpectedly')
            raw = json.loads(line)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SeekBackendError(f'Native Seeker IPC read failed: {exc}') from exc
        if not isinstance(raw, dict) or raw.get('protocol') != HOST_PROTOCOL or raw.get('command') != command or (raw.get('request_id') != request_id) or (raw.get('event') not in {'response', 'progress', 'terminal'}):
            raise SeekBackendError('Native Seeker protocol mismatch')
        return raw

    def _exchange_once(self, command: str, fields: dict[str, object] | None=None) -> dict[str, Any]:
        with self._lock:
            request_id = self._write_request(command, fields)
            event = self._read_event(command, request_id)
            if event['event'] != 'response':
                raise SeekBackendError('Native Seeker returned an unexpected event')
            self._raise_status(event)
            return event

    def seek(self, target_replay_time_ms: int, cancel: threading.Event, progress: Callable[[SeekProgress], None] | None=None, *, timeout_seconds: float=20 * 60, profile: SeekProfile, request_started_at: float | None=None) -> tuple[int, SeekMetrics]:
        if target_replay_time_ms < 0:
            raise ValueError('Target replay time cannot be negative')
        if timeout_seconds <= 0:
            raise ValueError('Seek timeout must be positive')
        now = time.monotonic()
        started_at = request_started_at if request_started_at is not None and request_started_at <= now else now
        fields = {'target_replay_time_ms': target_replay_time_ms, 'maximum_speed': profile.maximum_speed, 'far_poll_ms': max(int(round(profile.far_poll_seconds * 1000.0)), 1), 'timeout_ms': max(int(round(timeout_seconds * 1000.0)), 1), 'lower_process_priority': profile.lower_process_priority, 'high_qos': profile.high_qos, 'request_queue_age_ms': (now - started_at) * 1000.0}
        with self._lock:
            if not self._kernel32.ResetEvent(self._cancel_handle):
                error = ctypes.get_last_error()
                raise SeekBackendError(f'Could not reset Native Seeker cancellation channel (WinError {error})')
            if cancel.is_set():
                self.cancel()
            request_id = self._write_request('seek', fields)
            while True:
                event = self._read_event('seek', request_id)
                kind = event['event']
                if kind == 'progress':
                    if progress is not None:
                        try:
                            progress(SeekProgress(current_replay_time_ms=int(event['current_replay_time_ms']), target_replay_time_ms=int(event['target_replay_time_ms']), speed_value=int(event['speed_value']), stage=str(event['stage']), effective_speed=float(event['effective_speed']), eta_seconds=self._optional(event.get('eta_seconds')), process_cpu_percent=self._optional(event.get('process_cpu_percent')), command_latency_ms=float(event['command_latency_ms']), first_advance_ms=self._optional(event.get('first_advance_ms'))))
                        except Exception:
                            self.cancel()
                            try:
                                while event['event'] != 'terminal':
                                    event = self._read_event('seek', request_id)
                            except SeekBackendError:
                                LOGGER.warning('Native Seeker failed while draining after a progress callback error', exc_info=True)
                            raise
                    continue
                if kind != 'terminal':
                    raise SeekBackendError('Native Seeker returned an unexpected event')
                self._raise_status(event)
                position = int(event['final_replay_time_ms'])
                return (position, SeekMetrics(start_replay_time_ms=int(event['start_replay_time_ms']), final_replay_time_ms=position, target_replay_time_ms=int(event['target_replay_time_ms']), wall_duration_ms=float(event['wall_duration_ms']), command_latency_ms=float(event['command_latency_ms']), first_advance_ms=self._optional(event.get('first_advance_ms')), effective_speed=float(event['effective_speed']), process_cpu_percent=self._optional(event.get('process_cpu_percent')), overshoot_ms=int(event['overshoot_ms']), profile_key=profile.key, high_qos_applied=bool(event['high_qos_applied'])))

    def cancel(self) -> None:
        handle = getattr(self, '_cancel_handle', None)
        if handle and (not self._kernel32.SetEvent(handle)):
            LOGGER.warning('Could not signal replay transport cancellation: WinError %s', ctypes.get_last_error())

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self.cancel()
            process = self._process
            if process is not None and process.poll() is None:
                try:
                    self._exchange_once('shutdown')
                except (SeekBackendError, OSError):
                    LOGGER.warning('Replay transport did not accept graceful shutdown', exc_info=True)
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2.0)
            if process is not None:
                if process.stdin is not None:
                    process.stdin.close()
                if process.stdout is not None:
                    process.stdout.close()
            self._process = None
            self._closed = True
            stderr_log = getattr(self, '_stderr_log', None)
            if stderr_log is not None:
                stderr_log.close()
            handle = getattr(self, '_cancel_handle', None)
            if handle:
                self._kernel32.CloseHandle(handle)
                self._cancel_handle = None

    def __enter__(self) -> NativeReplayTransport:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
