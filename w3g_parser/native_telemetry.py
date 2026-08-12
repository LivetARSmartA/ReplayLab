from __future__ import annotations
import json
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from .diagnostics import get_logger, open_native_stderr
from .native_runtime import native_binary_candidates
from .seeker import SeekBackendError
HOST_PROTOCOL = 'replaylab-telemetry-v7'
HOST_MAX_TARGETS = 10
LOGGER = get_logger('skills_hud')
HOST_STATUS = {1: 'Skills HUD received an invalid request', 2: 'Skills HUD is not attached to Warcraft', 3: 'Skills HUD could not attach to Warcraft', 4: 'This Warcraft build is unsupported by Skills HUD', 5: "The selected player's hero is not available in Warcraft memory yet", 6: 'Skills HUD could not read the ability state', 7: 'Warcraft III has exited'}

class TelemetryHostError(SeekBackendError):

    def __init__(self, status: int, detail: int, message: str, snapshot: TelemetrySnapshot | None=None) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail
        self.snapshot = snapshot

def rawcode_value(rawcode: str) -> int:
    if len(rawcode) != 4:
        raise ValueError('Rawcode must contain four characters')
    try:
        payload = rawcode.encode('latin-1')
    except UnicodeEncodeError as exc:
        raise ValueError('Rawcode must use single-byte characters') from exc
    return int.from_bytes(payload, 'big')

def rawcode_text(value: int) -> str:
    try:
        result = int(value).to_bytes(4, 'big').decode('latin-1')
    except (OverflowError, UnicodeDecodeError) as exc:
        raise ValueError('Invalid rawcode value') from exc
    _validate_rawcode(result)
    return result

def _validate_rawcode(rawcode: str) -> str:
    if len(rawcode) != 4 or any((not 32 <= ord(character) <= 126 for character in rawcode)):
        raise ValueError('Rawcode must contain four printable characters')
    return rawcode

@dataclass(frozen=True)
class LiveAbilityState:
    rawcode: str
    level: int
    flags: int
    cooldown_ms: int

    @property
    def cooldown_seconds(self) -> float:
        return self.cooldown_ms / 1000.0

@dataclass(frozen=True)
class TelemetrySnapshot:
    process_id: int
    game_dll_base: int
    game_time_ms: int
    hero_address: int
    hero_rawcode: str
    player_slot: int
    selected_unit_address: int
    selected_unit_rawcode: str | None
    selected_player_slot: int | None
    abilities: tuple[LiveAbilityState, ...]
    memory_read_count: int
    memory_write_count: int
    invoked_spell_rawcodes: tuple[str, ...] = ()

def _native_host_candidates() -> list[Path]:
    return native_binary_candidates('replaylab_telemetry_host.exe', environment_variable='REPLAYLAB_TELEMETRY_HOST', build_subdirectory='telemetry')

def find_native_telemetry_host() -> Path:
    for candidate in _native_host_candidates():
        if candidate.is_file():
            return candidate.resolve()
    raise SeekBackendError('Skills HUD native component was not found. Reinstall ReplayLab.')

class NativeTelemetryHost:

    def __init__(self, player_slot: int, hero_rawcode: str, *, process_id: int=0, executable: Path | None=None) -> None:
        if os.name != 'nt':
            raise SeekBackendError('Skills HUD requires Windows')
        if process_id < 0 or process_id > 4294967295:
            raise ValueError('Process id is invalid')
        target = self._target_fields(player_slot, hero_rawcode)
        host = executable.resolve() if executable else find_native_telemetry_host()
        creation_flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        self._stderr_log = open_native_stderr('skills-hud')
        try:
            self._process: subprocess.Popen[str] = subprocess.Popen([str(host)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr_log, text=True, encoding='utf-8', errors='strict', bufsize=1, creationflags=creation_flags)
        except Exception:
            self._stderr_log.close()
            LOGGER.exception('Could not start telemetry host: %s', host)
            raise
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._request_id = 0
        self._closed = False
        self._last_status = 0
        try:
            self._exchange('configure', {'process_id': process_id, **target}, accepted_statuses=frozenset({5, 6}))
        except Exception:
            self.close()
            raise
        LOGGER.info('Telemetry host started: %s', host)

    @staticmethod
    def _target_fields(player_slot: int, hero_rawcode: str, hero_address: int=0) -> dict[str, object]:
        if not 0 <= player_slot <= 15:
            raise ValueError('Player slot must be in range 0..15')
        if not 0 <= hero_address <= 4294967295:
            raise ValueError('Hero address is invalid')
        return {'player_slot': player_slot, 'hero_rawcode': _validate_rawcode(hero_rawcode), 'hero_address': hero_address}

    @staticmethod
    def _decode_snapshot(payload: dict[str, Any]) -> TelemetrySnapshot:
        abilities = tuple((LiveAbilityState(rawcode=_validate_rawcode(str(row['rawcode'])), level=int(row['level']), flags=int(row['flags']), cooldown_ms=int(row['cooldown_ms'])) for row in payload.get('abilities', []) if isinstance(row, dict)))
        selected_rawcode = payload.get('selected_unit_rawcode')
        invoked = tuple((_validate_rawcode(str(rawcode)) for rawcode in payload.get('invoked_spell_rawcodes', [])))
        return TelemetrySnapshot(process_id=int(payload.get('process_id', 0)), game_dll_base=int(payload.get('game_dll_base', 0)), game_time_ms=int(payload.get('game_time_ms', 0)), hero_address=int(payload.get('hero_address', 0)), hero_rawcode=str(payload.get('hero_rawcode', '----')), player_slot=int(payload.get('player_slot', 0)), selected_unit_address=int(payload.get('selected_unit_address', 0)), selected_unit_rawcode=_validate_rawcode(str(selected_rawcode)) if selected_rawcode is not None else None, selected_player_slot=int(payload['selected_player_slot']) if payload.get('selected_player_slot') is not None else None, abilities=abilities, memory_read_count=int(payload.get('memory_read_count', 0)), memory_write_count=int(payload.get('memory_write_count', 0)), invoked_spell_rawcodes=invoked)

    def _exchange(self, command: str, fields: dict[str, object] | None=None, *, accept_error: bool=False, accepted_statuses: frozenset[int]=frozenset()) -> TelemetrySnapshot:
        with self._lock:
            if self._closed:
                raise SeekBackendError('Skills HUD is already closed')
            stdin = self._process.stdin
            stdout = self._process.stdout
            if stdin is None or stdout is None:
                raise SeekBackendError('Skills HUD IPC is unavailable')
            self._request_id += 1
            request_id = self._request_id
            request = {'protocol': HOST_PROTOCOL, 'request_id': request_id, 'command': command, **(fields or {})}
            try:
                stdin.write(json.dumps(request, ensure_ascii=False, separators=(',', ':')) + '\n')
                stdin.flush()
                line = stdout.readline()
                if not line:
                    raise SeekBackendError('Skills HUD terminated unexpectedly')
                response = json.loads(line)
            except (BrokenPipeError, OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise SeekBackendError(f'Skills HUD IPC failed: {exc}') from exc
            if not isinstance(response, dict) or response.get('protocol') != HOST_PROTOCOL or response.get('request_id') != request_id or (response.get('command') != command):
                raise SeekBackendError('Skills HUD protocol mismatch')
            status = int(response.get('status', 1))
            detail = int(response.get('detail', 0))
            snapshot = self._decode_snapshot(response)
            self._last_status = status
            if status and (not accept_error) and (status not in accepted_statuses):
                message = HOST_STATUS.get(status, f'telemetry error {status}')
                suffix = f' (WinError {detail})' if detail else ''
                LOGGER.warning('Telemetry command failed: command=%s status=%s detail=%s', command, status, detail)
                raise TelemetryHostError(status, detail, f'{message}{suffix}', snapshot)
            return snapshot

    @property
    def last_status(self) -> int:
        return self._last_status

    def set_target(self, player_slot: int, hero_rawcode: str, hero_address: int=0) -> TelemetrySnapshot:
        return self._exchange('set_target', self._target_fields(player_slot, hero_rawcode, hero_address))

    def prepare_targets(self, targets: list[tuple[int, str]]) -> TelemetrySnapshot:
        unique = list(dict.fromkeys(targets))[:HOST_MAX_TARGETS]
        return self._exchange('prepare_targets', {'targets': [self._target_fields(slot, rawcode) for slot, rawcode in unique]})

    def snapshot(self) -> TelemetrySnapshot:
        return self._exchange('snapshot')

    def ping(self) -> TelemetrySnapshot:
        return self._exchange('ping')

    def close(self) -> None:
        with self._close_lock:
            process = getattr(self, '_process', None)
            if process is None or self._closed:
                return
            if process.poll() is None:
                try:
                    self._exchange('shutdown', accept_error=True)
                except (SeekBackendError, OSError):
                    pass
            self._closed = True
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            if process.stdout is not None:
                try:
                    process.stdout.close()
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
            LOGGER.info('Telemetry host stopped: exit_code=%s', process.returncode)

    def __enter__(self) -> NativeTelemetryHost:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
