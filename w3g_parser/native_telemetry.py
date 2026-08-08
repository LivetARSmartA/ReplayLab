from __future__ import annotations
import os
import struct
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from .diagnostics import get_logger, open_native_stderr
from .native_runtime import native_binary_candidates
from .seeker import SeekBackendError
HOST_MAGIC = 1414286418
HOST_PROTOCOL_VERSION = 6
HOST_CONFIGURE = 1
HOST_SNAPSHOT = 2
HOST_SET_TARGET = 3
HOST_PING = 4
HOST_SHUTDOWN = 5
HOST_PREPARE_TARGETS = 7
HOST_MAX_ABILITIES = 64
HOST_MAX_TARGETS = 10
HOST_HEADER = struct.Struct('<IHHII')
HOST_CONFIG = struct.Struct('<IIII')
HOST_TARGET = struct.Struct('<III')
HOST_TARGETS = struct.Struct('<I' + 'III' * HOST_MAX_TARGETS)
HOST_RESPONSE_PREFIX = struct.Struct('<14IQQ')
HOST_ABILITY = struct.Struct('<4I')
HOST_RESPONSE_SIZE = HOST_RESPONSE_PREFIX.size + HOST_MAX_ABILITIES * HOST_ABILITY.size
LOGGER = get_logger('skills_hud')
HOST_STATUS = {1: 'Skills HUD получил неверный ответ', 2: 'Skills HUD ещё не подключён к Warcraft', 3: 'Не удалось подключить Skills HUD к Warcraft', 4: 'Эта сборка Warcraft не поддерживается Skills HUD', 5: 'Герой выбранного игрока пока не найден в памяти Warcraft', 6: 'Skills HUD не смог прочитать состояние способностей', 7: 'Warcraft III был закрыт'}

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
    if any((not 32 <= ord(character) <= 126 for character in result)):
        raise ValueError('Rawcode contains non-printable characters')
    return result

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
    raise SeekBackendError('Компонент Skills HUD не найден. Переустанови ReplayLab.')

class NativeTelemetryHost:

    def __init__(self, player_slot: int, hero_rawcode: str, *, process_id: int=0, executable: Path | None=None) -> None:
        if os.name != 'nt':
            raise SeekBackendError('Skills HUD работает только в Windows')
        target = self._target_payload(player_slot, hero_rawcode)
        if process_id < 0 or process_id > 4294967295:
            raise ValueError('Process id is invalid')
        host = executable.resolve() if executable else find_native_telemetry_host()
        creation_flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        self._stderr_log = open_native_stderr('skills-hud')
        try:
            self._process = subprocess.Popen([str(host)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr_log, bufsize=0, creationflags=creation_flags)
        except Exception:
            self._stderr_log.close()
            LOGGER.exception('Could not start telemetry host: %s', host)
            raise
        LOGGER.info('Telemetry host started: %s', host)
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._request_id = 0
        self._closed = False
        self._last_status = 0
        try:
            self._exchange(HOST_CONFIGURE, struct.pack('<I', process_id) + target, accepted_statuses=frozenset({5, 6}))
        except Exception:
            self.close()
            raise

    @staticmethod
    def _target_payload(player_slot: int, hero_rawcode: str, hero_address: int=0) -> bytes:
        if not 0 <= player_slot <= 15:
            raise ValueError('Player slot must be in range 0..15')
        if not 0 <= hero_address <= 4294967295:
            raise ValueError('Hero address is invalid')
        return HOST_TARGET.pack(player_slot, rawcode_value(hero_rawcode), hero_address)

    @staticmethod
    def _read_exact(stream: object, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = stream.read(remaining)
            if not chunk:
                raise SeekBackendError('Компонент Skills HUD неожиданно завершился')
            chunks.append(chunk)
            remaining -= len(chunk)
        return b''.join(chunks)

    @staticmethod
    def _decode_response(payload: bytes) -> tuple[int, int, TelemetrySnapshot]:
        if len(payload) != HOST_RESPONSE_SIZE:
            raise SeekBackendError('Skills HUD получил повреждённый ответ')
        status, detail, process_id, game_dll_base, game_time_ms, hero_address, hero_value, player_slot, selected_unit_address, selected_unit_value, selected_player_slot_value, invoked_spell_value_1, invoked_spell_value_2, ability_count, memory_read_count, memory_write_count = HOST_RESPONSE_PREFIX.unpack_from(payload)
        if ability_count > HOST_MAX_ABILITIES:
            raise SeekBackendError('Skills HUD получил слишком много способностей')
        abilities: list[LiveAbilityState] = []
        offset = HOST_RESPONSE_PREFIX.size
        for index in range(ability_count):
            raw_value, level, flags, cooldown_ms = HOST_ABILITY.unpack_from(payload, offset + index * HOST_ABILITY.size)
            abilities.append(LiveAbilityState(rawcode=rawcode_text(raw_value), level=level, flags=flags, cooldown_ms=cooldown_ms))
        hero_rawcode = '----' if hero_value == 0 else rawcode_text(hero_value)
        selected_unit_rawcode = rawcode_text(selected_unit_value) if selected_unit_value != 0 else None
        selected_player_slot = selected_player_slot_value if selected_player_slot_value <= 15 else None
        invoked_spell_rawcodes = tuple((rawcode_text(value) for value in (invoked_spell_value_1, invoked_spell_value_2) if value != 0))
        return (status, detail, TelemetrySnapshot(process_id=process_id, game_dll_base=game_dll_base, game_time_ms=game_time_ms, hero_address=hero_address, hero_rawcode=hero_rawcode, player_slot=player_slot, selected_unit_address=selected_unit_address, selected_unit_rawcode=selected_unit_rawcode, selected_player_slot=selected_player_slot, abilities=tuple(abilities), memory_read_count=memory_read_count, memory_write_count=memory_write_count, invoked_spell_rawcodes=invoked_spell_rawcodes))

    def _exchange(self, command: int, payload: bytes=b'', *, accept_error: bool=False, accepted_statuses: frozenset[int]=frozenset()) -> TelemetrySnapshot:
        with self._lock:
            if self._closed:
                raise SeekBackendError('Skills HUD уже выключен')
            stdin = self._process.stdin
            stdout = self._process.stdout
            if stdin is None or stdout is None:
                raise SeekBackendError('Связь со Skills HUD недоступна')
            self._request_id += 1
            request_id = self._request_id
            try:
                stdin.write(HOST_HEADER.pack(HOST_MAGIC, HOST_PROTOCOL_VERSION, command, request_id, len(payload)))
                if payload:
                    stdin.write(payload)
                stdin.flush()
                response_header = self._read_exact(stdout, HOST_HEADER.size)
                magic, version, response_command, response_id, response_size = HOST_HEADER.unpack(response_header)
                if magic != HOST_MAGIC or version != HOST_PROTOCOL_VERSION or response_command != command or (response_id != request_id) or (response_size != HOST_RESPONSE_SIZE):
                    raise SeekBackendError('Компонент Skills HUD несовместим с программой')
                response = self._read_exact(stdout, response_size)
            except (BrokenPipeError, OSError) as exc:
                raise SeekBackendError(f'Skills HUD потерял связь: {exc}') from exc
            status, detail, snapshot = self._decode_response(response)
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
        return self._exchange(HOST_SET_TARGET, self._target_payload(player_slot, hero_rawcode, hero_address))

    def prepare_targets(self, targets: list[tuple[int, str]]) -> TelemetrySnapshot:
        unique = list(dict.fromkeys(targets))[:HOST_MAX_TARGETS]
        values: list[int] = [len(unique)]
        for player_slot, hero_rawcode in unique:
            if not 0 <= player_slot <= 15:
                raise ValueError('Player slot must be in range 0..15')
            values.extend((player_slot, rawcode_value(hero_rawcode), 0))
        values.extend([0] * (HOST_MAX_TARGETS - len(unique)) * 3)
        return self._exchange(HOST_PREPARE_TARGETS, HOST_TARGETS.pack(*values))

    def snapshot(self) -> TelemetrySnapshot:
        return self._exchange(HOST_SNAPSHOT)

    def ping(self) -> TelemetrySnapshot:
        return self._exchange(HOST_PING)

    def close(self) -> None:
        with self._close_lock:
            process = getattr(self, '_process', None)
            if process is None or self._closed:
                return
            if process.poll() is None:
                try:
                    self._exchange(HOST_SHUTDOWN, accept_error=True)
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
            stderr_log = getattr(self, '_stderr_log', None)
            if stderr_log is not None:
                stderr_log.close()
            LOGGER.info('Telemetry host stopped: exit_code=%s', process.returncode)

    def __enter__(self) -> NativeTelemetryHost:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
