from __future__ import annotations
import os
import struct
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from .seeker import CameraRuntimeSession, CameraState, SeekBackendError
HOST_MAGIC = 1296256082
HOST_PROTOCOL_VERSION = 4
HOST_CONFIGURE = 1
HOST_SET_TARGET = 2
HOST_BEGIN_TRANSITION = 3
HOST_UPDATE_SUBJECT = 4
HOST_PING = 5
HOST_SHUTDOWN = 6
HOST_SYNC_POSE = 7
HOST_HEADER = struct.Struct('<IHHII')
HOST_RESPONSE = struct.Struct('<II7dQQQQII')
HOST_CONFIG = struct.Struct('<II7I10d')
HOST_TARGET = struct.Struct('<8d')
HOST_POSE = struct.Struct('<7d')
HOST_TRANSITION = struct.Struct('<7d')
HOST_SUBJECT = struct.Struct('<2d')
HOST_STATUS = {1: 'Native Camera Host отклонил IPC-команду', 2: 'Native Camera Host находится в неподходящем состоянии', 3: 'Native Camera Host не смог открыть процесс Warcraft', 4: 'Native Camera Host не смог прочитать камеру Warcraft', 5: 'Native Camera Host не смог записать состояние камеры Warcraft', 6: 'Native Camera Host обнаружил небезопасное состояние камеры', 7: 'Native Camera Host отклонил небезопасную команду движения', 8: 'Windows не предоставила высокоточный таймер для Camera Engine', 9: 'Warcraft III был закрыт — Camera Engine остановлен', 10: 'Native Camera Host не успевает принимать команды камеры'}

def select_camera_update_hz(max_fps: object, refresh_rate: object, *, default: int=120) -> int:
    rates = [int(value) for value in (max_fps, refresh_rate) if isinstance(value, int) and value > 0]
    selected = min(rates) if rates else default
    return min(max(selected, 30), 180)

def configured_camera_update_hz() -> int:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Software\\Blizzard Entertainment\\Warcraft III\\Video') as key:
            values: dict[str, object] = {}
            for name in ('maxfps', 'refreshrate'):
                try:
                    values[name] = winreg.QueryValueEx(key, name)[0]
                except OSError:
                    values[name] = None
        return select_camera_update_hz(values['maxfps'], values['refreshrate'])
    except (ImportError, OSError):
        return 120

@dataclass(frozen=True)
class CameraTransitionCommand:
    subject_x: float
    subject_y: float
    distance_delta: float
    pitch_delta: float
    z_offset_delta: float
    duration_seconds: float
    target_response: float = 0.0

@dataclass(frozen=True)
class CameraSafetyLimits:
    target_x_min: float
    target_x_max: float
    target_y_min: float
    target_y_max: float
    distance_min: float
    distance_max: float
    pitch_min: float
    pitch_max: float
    z_offset_min: float
    z_offset_max: float

@dataclass(frozen=True)
class NativeCameraMetrics:
    tick_count: int = 0
    late_tick_count: int = 0
    command_count: int = 0
    memory_write_count: int = 0
    max_lateness_us: int = 0
    max_work_us: int = 0

def _native_host_candidates() -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get('REPLAYLAB_CAMERA_HOST')
    if configured:
        candidates.append(Path(configured))
    bundle_root = getattr(sys, '_MEIPASS', None)
    if bundle_root:
        candidates.append(Path(bundle_root) / 'native' / 'replaylab_camera_host.exe')
    candidates.append(Path(sys.executable).resolve().parent / '_internal' / 'native' / 'replaylab_camera_host.exe')
    project_root = Path(__file__).resolve().parents[1]
    candidates.append(project_root / 'native' / 'replaylab_camera_host.exe')
    candidates.append(project_root / 'build' / 'native' / 'camera_motion' / 'replaylab_camera_host.exe')
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve()).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique

def find_native_camera_host() -> Path:
    for candidate in _native_host_candidates():
        if candidate.is_file():
            return candidate.resolve()
    raise SeekBackendError('Нативный Camera Host не найден. Камера не запущена: production-режим без C++-движка не поддерживается.')

class NativeCameraHost:

    def __init__(self, session: CameraRuntimeSession, *, update_hz: int=120, limits: CameraSafetyLimits, executable: Path | None=None) -> None:
        if os.name != 'nt':
            raise SeekBackendError('Native Camera Host requires Windows')
        if not 30 <= update_hz <= 240:
            raise ValueError('Native update rate must be in range 30..240')
        host = executable.resolve() if executable else find_native_camera_host()
        creation_flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        self._process = subprocess.Popen([str(host)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0, creationflags=creation_flags)
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._request_id = 0
        self._closed = False
        self._metrics = NativeCameraMetrics()
        try:
            payload = HOST_CONFIG.pack(session.process_id, update_hz, session.target_x_address, session.target_y_address, session.distance_address, session.yaw_address, session.pitch_address, session.roll_address, session.z_offset_address, limits.target_x_min, limits.target_x_max, limits.target_y_min, limits.target_y_max, limits.distance_min, limits.distance_max, limits.pitch_min, limits.pitch_max, limits.z_offset_min, limits.z_offset_max)
            self._exchange(HOST_CONFIGURE, payload)
        except Exception:
            self.close()
            raise

    @staticmethod
    def _read_exact(stream: object, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = stream.read(remaining)
            if not chunk:
                raise SeekBackendError('Native Camera Host terminated unexpectedly')
            chunks.append(chunk)
            remaining -= len(chunk)
        return b''.join(chunks)

    @staticmethod
    def _pose_values(state: CameraState) -> tuple[float, ...]:
        return (state.target_x, state.target_y, state.distance, state.yaw, state.pitch, state.roll, state.z_offset)

    def _exchange(self, command: int, payload: bytes=b'', *, accept_error: bool=False) -> CameraState:
        with self._lock:
            if self._closed:
                raise SeekBackendError('Native Camera Host is closed')
            stdin = self._process.stdin
            stdout = self._process.stdout
            if stdin is None or stdout is None:
                raise SeekBackendError('Native Camera Host IPC is unavailable')
            self._request_id += 1
            request_id = self._request_id
            header = HOST_HEADER.pack(HOST_MAGIC, HOST_PROTOCOL_VERSION, command, request_id, len(payload))
            try:
                stdin.write(header)
                if payload:
                    stdin.write(payload)
                stdin.flush()
                response_header = self._read_exact(stdout, HOST_HEADER.size)
                magic, version, response_command, response_id, response_size = HOST_HEADER.unpack(response_header)
                if magic != HOST_MAGIC or version != HOST_PROTOCOL_VERSION or response_command != command or (response_id != request_id) or (response_size != HOST_RESPONSE.size):
                    raise SeekBackendError('Native Camera Host protocol mismatch')
                response = HOST_RESPONSE.unpack(self._read_exact(stdout, response_size))
            except (BrokenPipeError, OSError) as exc:
                raise SeekBackendError(f'Native Camera Host IPC failed: {exc}') from exc
            status, detail, *values = response
            pose = values[:7]
            self._metrics = NativeCameraMetrics(*values[7:])
            if status and (not accept_error):
                message = HOST_STATUS.get(status, f'native host error {status}')
                suffix = f' (WinError {detail})' if detail else ''
                raise SeekBackendError(f'{message}{suffix}')
            return CameraState(target_x=pose[0], target_y=pose[1], distance=pose[2], yaw=pose[3], pitch=pose[4], roll=pose[5], z_offset=pose[6])

    def set_target(self, state: CameraState, response: float) -> CameraState:
        payload = HOST_TARGET.pack(*self._pose_values(state), response)
        return self._exchange(HOST_SET_TARGET, payload)

    def sync_pose(self, state: CameraState) -> CameraState:
        return self._exchange(HOST_SYNC_POSE, HOST_POSE.pack(*self._pose_values(state)))

    def begin_transition(self, command: CameraTransitionCommand) -> CameraState:
        payload = HOST_TRANSITION.pack(command.subject_x, command.subject_y, command.distance_delta, command.pitch_delta, command.z_offset_delta, command.duration_seconds, command.target_response)
        return self._exchange(HOST_BEGIN_TRANSITION, payload)

    def update_subject(self, x: float, y: float) -> CameraState:
        return self._exchange(HOST_UPDATE_SUBJECT, HOST_SUBJECT.pack(x, y))

    def ping(self) -> CameraState:
        return self._exchange(HOST_PING)

    def metrics(self) -> NativeCameraMetrics:
        return self._metrics

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
