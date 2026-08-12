from __future__ import annotations
import json
import math
import os
import subprocess
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from .diagnostics import get_logger, open_native_stderr
from .native_runtime import native_binary_candidates
from .seeker import CameraState, SeekBackendError, SeekCancelled
HOST_PROTOCOL = 'replaylab-camera-v9'
LOGGER = get_logger('camera')
HOST_STATUS = {1: 'Camera Engine received an invalid request', 2: 'Camera Engine is not configured', 3: 'Camera Engine could not attach to Warcraft', 4: 'Camera Engine could not read the camera state', 5: 'Camera Engine could not write the camera state', 6: 'Camera Engine stopped because the pose was unsafe', 7: 'Camera Engine rejected a motion command', 8: 'Windows could not start the high-resolution camera timer', 9: 'Warcraft III exited; Camera Engine stopped', 10: 'Camera Engine command queue is full', 11: 'This Warcraft build is unsupported by Camera Engine', 12: 'Camera Engine capability was not found in Warcraft memory', 13: 'The requested camera subject is no longer available'}

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
class DroneSettings:
    move_speed: float = 55.0
    lift_speed: float = 1400.0
    dolly_speed: float = 1800.0
    yaw_speed: float = 0.9
    orbit_speed_degrees: float = 18.0
    pitch_speed: float = 0.65
    acceleration_response: float = 3.5
    braking_response: float = 6.0
    follow_response: float = 8.0
    bank_angle: float = 0.08
    bank_response: float = 5.0
    prediction_seconds: float = 0.05
    wobble_position: float = 0.0
    wobble_roll: float = 0.0

@dataclass(frozen=True)
class DroneInput:
    forward: float = 0.0
    strafe: float = 0.0
    lift: float = 0.0
    yaw: float = 0.0
    pitch: float = 0.0
    dolly: float = 0.0
    subject_x: float = 0.0
    subject_y: float = 0.0
    subject_velocity_x: float = 0.0
    subject_velocity_y: float = 0.0
    target_lock: bool = False

@dataclass(frozen=True)
class NativeCameraMetrics:
    tick_count: int = 0
    late_tick_count: int = 0
    command_count: int = 0
    memory_write_count: int = 0
    max_lateness_us: int = 0
    max_work_us: int = 0

@dataclass(frozen=True)
class CameraSubject:
    id: int
    rawcode: str
    player_slot: int
    x: float
    y: float

def _native_host_candidates() -> list[Path]:
    return native_binary_candidates('replaylab_camera_host.exe', environment_variable='REPLAYLAB_CAMERA_HOST', build_subdirectory='camera_motion')

def find_native_camera_host() -> Path:
    for candidate in _native_host_candidates():
        if candidate.is_file():
            return candidate.resolve()
    raise SeekBackendError('Camera Engine native component was not found. Reinstall ReplayLab.')

class NativeCameraHost:

    def __init__(self, process_id: int=0, *, executable: Path | None=None) -> None:
        if os.name != 'nt':
            raise SeekBackendError('Native Camera Host requires Windows')
        if process_id < 0 or process_id > 4294967295:
            raise ValueError('Process id is invalid')
        host = executable.resolve() if executable else find_native_camera_host()
        creation_flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        self._stderr_log = open_native_stderr('camera')
        try:
            self._process: subprocess.Popen[str] = subprocess.Popen([str(host)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr_log, text=True, encoding='utf-8', errors='strict', bufsize=1, creationflags=creation_flags)
        except Exception:
            self._stderr_log.close()
            LOGGER.exception('Could not start camera host: %s', host)
            raise
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._request_id = 0
        self._closed = False
        self._metrics = NativeCameraMetrics()
        self._process_id = 0
        self._profile_key = ''
        self._configured = False
        try:
            state, response = self._exchange_payload('discover', {'process_id': process_id})
            self._last_state = state
            self._process_id = int(response['process_id'])
            self._profile_key = str(response['profile_key'])
            if self._process_id <= 0 or not self._profile_key:
                raise SeekBackendError('Camera Engine returned an invalid runtime capability')
        except Exception:
            self.close()
            raise
        LOGGER.info('Camera host started: %s', host)

    @staticmethod
    def _state_fields(state: CameraState) -> dict[str, float]:
        return {'target_x': state.target_x, 'target_y': state.target_y, 'distance': state.distance, 'yaw': state.yaw, 'pitch': state.pitch, 'roll': state.roll, 'z_offset': state.z_offset}

    def _exchange_payload(self, command: str, fields: dict[str, object] | None=None, *, accept_error: bool=False) -> tuple[CameraState, dict[str, Any]]:
        with self._lock:
            if self._closed:
                raise SeekBackendError('Native Camera Host is closed')
            stdin = self._process.stdin
            stdout = self._process.stdout
            if stdin is None or stdout is None:
                raise SeekBackendError('Native Camera Host IPC is unavailable')
            self._request_id += 1
            request_id = self._request_id
            request = {'protocol': HOST_PROTOCOL, 'request_id': request_id, 'command': command, **(fields or {})}
            try:
                stdin.write(json.dumps(request, ensure_ascii=False, separators=(',', ':')) + '\n')
                stdin.flush()
                line = stdout.readline()
                if not line:
                    raise SeekBackendError('Native Camera Host terminated unexpectedly')
                response = json.loads(line)
            except (BrokenPipeError, OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise SeekBackendError(f'Native Camera Host IPC failed: {exc}') from exc
            if not isinstance(response, dict) or response.get('protocol') != HOST_PROTOCOL or response.get('request_id') != request_id or (response.get('command') != command):
                raise SeekBackendError('Native Camera Host protocol mismatch')
            status = int(response.get('status', 1))
            detail = int(response.get('detail', 0))
            raw_state = response.get('state')
            raw_metrics = response.get('metrics')
            if not isinstance(raw_state, dict) or not isinstance(raw_metrics, dict):
                raise SeekBackendError('Native Camera Host response is invalid')
            state = CameraState(**{key: float(raw_state[key]) for key in ('target_x', 'target_y', 'distance', 'yaw', 'pitch', 'roll', 'z_offset')})
            self._metrics = NativeCameraMetrics(**{key: int(raw_metrics[key]) for key in ('tick_count', 'late_tick_count', 'command_count', 'memory_write_count', 'max_lateness_us', 'max_work_us')})
            if status and (not accept_error):
                message = HOST_STATUS.get(status, f'native host error {status}')
                suffix = f' (WinError {detail})' if detail else ''
                LOGGER.warning('Camera command failed: command=%s status=%s detail=%s', command, status, detail)
                raise SeekBackendError(f'{message}{suffix}')
            self._last_state = state
            return (state, response)

    def _exchange(self, command: str, fields: dict[str, object] | None=None, *, accept_error: bool=False) -> CameraState:
        state, _ = self._exchange_payload(command, fields, accept_error=accept_error)
        return state

    @staticmethod
    def _subject_from(response: dict[str, Any]) -> CameraSubject:
        raw_subject = response.get('subject')
        if not isinstance(raw_subject, dict):
            raise SeekBackendError('Camera Engine subject response is invalid')
        rawcode = str(raw_subject.get('rawcode', ''))
        if len(rawcode) != 4:
            raise SeekBackendError('Camera Engine returned an invalid rawcode')
        subject = CameraSubject(id=int(raw_subject.get('id', 0)), rawcode=rawcode, player_slot=int(raw_subject.get('player_slot', -1)), x=float(raw_subject.get('x', math.nan)), y=float(raw_subject.get('y', math.nan)))
        if subject.id <= 0 or not 0 <= subject.player_slot <= 15 or (not math.isfinite(subject.x)) or (not math.isfinite(subject.y)):
            raise SeekBackendError('Camera Engine returned an unsafe subject')
        return subject

    @property
    def process_id(self) -> int:
        return self._process_id

    @property
    def profile_key(self) -> str:
        return self._profile_key

    def camera_state(self) -> CameraState:
        if not self._configured:
            return self._last_state
        return self.ping()

    def configure(self, *, update_hz: int, limits: CameraSafetyLimits) -> CameraState:
        if self._configured:
            raise SeekBackendError('Camera Engine is already configured')
        if not 30 <= update_hz <= 240:
            raise ValueError('Native update rate must be in range 30..240')
        state = self._exchange('configure', {'update_hz': update_hz, **asdict(limits)})
        self._configured = True
        return state

    def unlock_camera(self, maximum_distance: float=100000.0) -> None:
        self._exchange('unlock_camera', {'maximum_distance': maximum_distance})

    def selected_unit(self) -> tuple[int, str]:
        _, response = self._exchange_payload('selected_subject')
        subject = self._subject_from(response)
        return (subject.id, subject.rawcode)

    def find_player_hero(self, player_slot: int, hero_rawcode: str, cancel: threading.Event | None=None) -> tuple[int, str]:
        if cancel is not None and cancel.is_set():
            raise SeekCancelled('Hero lookup was cancelled')
        _, response = self._exchange_payload('find_player_hero', {'player_slot': player_slot, 'hero_rawcode': hero_rawcode})
        if cancel is not None and cancel.is_set():
            raise SeekCancelled('Hero lookup was cancelled')
        subject = self._subject_from(response)
        return (subject.id, subject.rawcode)

    def unit_camera_position(self, subject_id: int) -> tuple[float, float]:
        _, response = self._exchange_payload('subject_position', {'subject_id': subject_id})
        subject = self._subject_from(response)
        return (subject.x, subject.y)

    def set_target(self, state: CameraState, response: float) -> CameraState:
        return self._exchange('set_target', {**self._state_fields(state), 'response': response})

    def sync_pose(self, state: CameraState) -> CameraState:
        return self._exchange('sync_pose', self._state_fields(state))

    def begin_transition(self, command: CameraTransitionCommand) -> CameraState:
        return self._exchange('begin_transition', asdict(command))

    def update_subject(self, x: float, y: float) -> CameraState:
        return self._exchange('update_subject', {'x': x, 'y': y})

    def configure_drone(self, settings: DroneSettings) -> CameraState:
        values = asdict(settings)
        values.pop('orbit_speed_degrees', None)
        return self._exchange('configure_drone', values)

    def enter_drone(self, state: CameraState) -> CameraState:
        return self._exchange('enter_drone', self._state_fields(state))

    def set_drone_input(self, value: DroneInput) -> CameraState:
        return self._exchange('set_drone_input', asdict(value))

    def exit_drone(self) -> CameraState:
        return self._exchange('exit_drone')

    def turn_drone(self, angle_radians: float) -> CameraState:
        if not math.isfinite(angle_radians) or abs(angle_radians) > math.tau:
            raise ValueError('Drone turn angle must be finite and within 360 degrees.')
        return self._exchange('turn_drone', {'angle_radians': angle_radians})

    def ping(self) -> CameraState:
        return self._exchange('ping')

    def metrics(self) -> NativeCameraMetrics:
        return self._metrics

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
            LOGGER.info('Camera host stopped: exit_code=%s', process.returncode)
