from __future__ import annotations
import ctypes
import math
import os
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, replace
from typing import Callable
from .camera_input import CameraInputRouter
from .camera_modes import CAMERA_TRANSITION_PRESETS, CameraTransitionKind, CameraTransitionSpec, CameraRigMode, DirectorFrame, OrbitDirector, SmartFollowDirector, hero_switch_transition
from .native_camera import CameraSafetyLimits, CameraTransitionCommand, DroneInput, DroneSettings, NativeCameraHost, configured_camera_update_hz
from .seeker import CameraState, SeekBackendError, Warcraft126MemoryBackend

@dataclass(frozen=True)
class CameraMotionSettings:
    move_speed: float = 55.0
    rotation_speed: float = 1.0
    zoom_speed: float = 2200.0
    lift_speed: float = 1760.0
    smoothing: float = 6.0
    follow_smoothing: float = 5.0
    update_hz: int = 60

def standard_view_at(home: CameraState, current: CameraState) -> CameraState:
    return replace(home, target_x=current.target_x, target_y=current.target_y)

def orbit_dolly_axis(distance: float, target_distance: float | None, target_velocity: float, dolly_speed: float) -> float:
    if target_distance is None:
        return 0.0
    if not all((math.isfinite(value) for value in (distance, target_distance, target_velocity, dolly_speed))) or dolly_speed <= 0.0:
        raise ValueError('Orbit dolly values are invalid')
    error = target_distance - distance
    if abs(error) < 0.75 and abs(target_velocity) < 1.0:
        return 0.0
    commanded_velocity = target_velocity * 0.4 + error
    return min(max(commanded_velocity / dolly_speed, -1.0), 1.0)

def camera_safety_limits(home: CameraState) -> CameraSafetyLimits:

    def map_limits(value: float) -> tuple[float, float]:
        if -128.0 <= value <= 640.0:
            return (-128.0, 640.0)
        return (value - 512.0, value + 512.0)
    target_x_min, target_x_max = map_limits(home.target_x)
    target_y_min, target_y_max = map_limits(home.target_y)
    minimum_distance = max(900.0, min(home.distance * 0.4, 1400.0))
    maximum_distance = min(max(5000.0, home.distance * 2.0), 6500.0)
    return CameraSafetyLimits(target_x_min=max(-100000.0, target_x_min), target_x_max=min(100000.0, target_x_max), target_y_min=max(-100000.0, target_y_min), target_y_max=min(100000.0, target_y_max), distance_min=minimum_distance, distance_max=maximum_distance, pitch_min=home.pitch - 0.5, pitch_max=home.pitch + 0.5, z_offset_min=home.z_offset - 250.0, z_offset_max=home.z_offset + 600.0)

def camera_transition_command(current: CameraState, limits: CameraSafetyLimits, subject_x: float, subject_y: float, spec: CameraTransitionSpec) -> CameraTransitionCommand:
    target_distance = min(max(current.distance + spec.distance_delta, limits.distance_min), limits.distance_max)
    target_pitch = min(max(current.pitch + spec.pitch_delta, limits.pitch_min), limits.pitch_max)
    target_z_offset = min(max(current.z_offset + spec.z_offset_delta, limits.z_offset_min), limits.z_offset_max)
    return CameraTransitionCommand(subject_x=subject_x, subject_y=subject_y, distance_delta=target_distance - current.distance, pitch_delta=target_pitch - current.pitch, z_offset_delta=target_z_offset - current.z_offset, duration_seconds=spec.duration_seconds, target_response=spec.target_response)

@dataclass(frozen=True)
class ActiveCameraTransition:
    kind: CameraTransitionKind
    subject_address: int | None
    subject_label: str
    deadline: float

class SmoothCameraController:
    VK_PRIOR = 33
    VK_NEXT = 34
    VK_END = 35
    VK_HOME = 36
    VK_INSERT = 45
    VK_DELETE = 46
    VK_LEFT = 37
    VK_UP = 38
    VK_RIGHT = 39
    VK_DOWN = 40
    CONTROL_KEYS = {VK_PRIOR, VK_NEXT, VK_END, VK_HOME, VK_INSERT, VK_DELETE, VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN}

    def __init__(self, backend: Warcraft126MemoryBackend, input_router: CameraInputRouter, *, on_error: Callable[[str], None] | None=None, on_state: Callable[[CameraState], None] | None=None, on_follow_lost: Callable[[str], None] | None=None) -> None:
        if os.name != 'nt':
            raise SeekBackendError('Camera controller requires Windows')
        self._backend = backend
        self._input_router = input_router
        self._on_error = on_error
        self._on_state = on_state
        self._on_follow_lost = on_follow_lost
        self._settings = CameraMotionSettings()
        self._settings_lock = threading.Lock()
        self._follow_lock = threading.Lock()
        self._follow_unit: int | None = None
        self._smart_follow = False
        self._drone_lock = threading.Lock()
        self._drone_enabled = False
        self._drone_target_lock = False
        self._orbit_enabled = False
        self._orbit_manual_dolly = False
        self._drone_settings = DroneSettings()
        self._orbit_director = OrbitDirector()
        self._transition_lock = threading.Lock()
        self._transition: ActiveCameraTransition | None = None
        self._transition_return_state: CameraState | None = None
        self._transition_kind: CameraTransitionKind | None = None
        self._transition_label: str | None = None
        self._transition_duration = 0.0
        self._motion_lock = threading.Lock()
        self._stop = threading.Event()
        self._reset_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._user32 = ctypes.WinDLL('user32', use_last_error=True)
        self._home_state = backend.camera_state()
        self._safety = camera_safety_limits(self._home_state)
        self._native_update_hz = configured_camera_update_hz()
        self._native = NativeCameraHost(backend.camera_runtime_session(), update_hz=self._native_update_hz, limits=self._safety)
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def following(self) -> bool:
        return self._follow_address() is not None

    @property
    def smart_following(self) -> bool:
        with self._follow_lock:
            return self._smart_follow

    @property
    def drone_enabled(self) -> bool:
        with self._drone_lock:
            return self._drone_enabled

    @property
    def drone_target_locked(self) -> bool:
        with self._drone_lock:
            return self._drone_enabled and self._drone_target_lock

    @property
    def orbit_enabled(self) -> bool:
        with self._drone_lock:
            return self._drone_enabled and self._drone_target_lock and self._orbit_enabled

    @property
    def orbit_direction(self) -> int:
        with self._drone_lock:
            return self._orbit_director.direction

    @property
    def orbit_ring_index(self) -> int:
        with self._drone_lock:
            return self._orbit_director.ring_index

    @property
    def native_update_hz(self) -> int:
        return self._native_update_hz

    @property
    def rig_mode(self) -> CameraRigMode:
        if self.orbit_enabled:
            return CameraRigMode.ORBIT
        if self.drone_enabled:
            return CameraRigMode.DRONE
        with self._transition_lock:
            if self._transition is not None or self._transition_return_state is not None:
                return CameraRigMode.CINEMATIC
        with self._follow_lock:
            if self._smart_follow:
                return CameraRigMode.SMART_FOLLOW
            if self._follow_unit is not None:
                return CameraRigMode.FOLLOW
        return CameraRigMode.FREE

    def update_settings(self, *, move_speed: float | None=None, rotation_speed: float | None=None, zoom_speed: float | None=None, lift_speed: float | None=None, smoothing: float | None=None, follow_smoothing: float | None=None) -> None:
        with self._settings_lock:
            current = self._settings
            values = {'move_speed': current.move_speed if move_speed is None else move_speed, 'rotation_speed': current.rotation_speed if rotation_speed is None else rotation_speed, 'zoom_speed': current.zoom_speed if zoom_speed is None else zoom_speed, 'lift_speed': current.lift_speed if lift_speed is None else lift_speed, 'smoothing': current.smoothing if smoothing is None else smoothing, 'follow_smoothing': current.follow_smoothing if follow_smoothing is None else follow_smoothing}
            if not 1 <= values['move_speed'] <= 500:
                raise ValueError('Move speed must be in range 1..500')
            if not 0.05 <= values['rotation_speed'] <= 10:
                raise ValueError('Rotation speed must be in range 0.05..10')
            if not 100 <= values['zoom_speed'] <= 50000:
                raise ValueError('Zoom speed must be in range 100..50000')
            if not 10 <= values['lift_speed'] <= 50000:
                raise ValueError('Lift speed must be in range 10..50000')
            if not 0.5 <= values['smoothing'] <= 30:
                raise ValueError('Smoothing must be in range 0.5..30')
            if not 0.5 <= values['follow_smoothing'] <= 30:
                raise ValueError('Follow smoothing must be in range 0.5..30')
            self._settings = replace(current, **values)

    def update_drone_settings(self, settings: DroneSettings) -> None:
        with self._drone_lock:
            self._orbit_director.set_speed(settings.orbit_speed_degrees)
        with self._motion_lock:
            self._native.configure_drone(settings)
        with self._drone_lock:
            self._drone_settings = settings

    def toggle_drone(self) -> bool:
        with self._drone_lock:
            active = self._drone_enabled
            settings = self._drone_settings
        self._clear_transition()
        with self._motion_lock:
            if active:
                self._native.exit_drone()
            else:
                current = self._backend.camera_state()
                self._native.configure_drone(settings)
                self._native.enter_drone(current)
        with self._drone_lock:
            self._drone_enabled = not active
            self._drone_target_lock = False
            self._orbit_enabled = False
            self._orbit_manual_dolly = False
            self._orbit_director.deactivate()
            enabled = self._drone_enabled
        if enabled:
            with self._follow_lock:
                self._smart_follow = False
        self._input_router.set_follow_active(enabled or self._follow_address() is not None)
        return enabled

    def toggle_drone_target_lock(self) -> bool:
        if not self.drone_enabled:
            raise SeekBackendError('Сначала включи Fly Drone.')
        if self._follow_address() is None:
            raise SeekBackendError('Сначала выбери героя в слотах Follow.')
        with self._drone_lock:
            self._drone_target_lock = not self._drone_target_lock
            if not self._drone_target_lock:
                self._orbit_enabled = False
                self._orbit_manual_dolly = False
                self._orbit_director.deactivate()
            return self._drone_target_lock

    def toggle_orbit(self) -> bool:
        if self._follow_address() is None:
            raise SeekBackendError('Сначала выбери героя в слотах Follow.')
        if not self.drone_enabled:
            self.toggle_drone()
        with self._motion_lock:
            current = self._native.ping()
        with self._drone_lock:
            self._orbit_enabled = not self._orbit_enabled
            if self._orbit_enabled:
                self._drone_target_lock = True
                self._orbit_manual_dolly = False
                self._orbit_director.activate(current.distance, self._safety.distance_min, self._safety.distance_max)
            else:
                self._orbit_manual_dolly = False
                self._orbit_director.deactivate()
            return self._orbit_enabled

    def reverse_orbit(self) -> int:
        with self._drone_lock:
            if not (self._drone_enabled and self._drone_target_lock and self._orbit_enabled):
                raise SeekBackendError('Сначала включи Orbit.')
            return self._orbit_director.reverse()

    def shift_orbit_ring(self, step: int) -> int:
        with self._motion_lock:
            current = self._native.ping()
        with self._drone_lock:
            if not (self._drone_enabled and self._drone_target_lock and self._orbit_enabled):
                raise SeekBackendError('Сначала включи Orbit.')
            return self._orbit_director.shift_ring(step, current.distance)

    def turn_drone(self, angle_degrees: float) -> None:
        if not self.drone_enabled:
            raise SeekBackendError('Сначала включи Fly Drone.')
        angle_radians = math.radians(angle_degrees)
        if not math.isfinite(angle_radians) or abs(angle_radians) > math.tau:
            raise ValueError('Угол поворота Drone должен быть в пределах 360°.')
        with self._motion_lock:
            self._native.turn_drone(angle_radians)

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='war3-smooth-camera', daemon=True)
        self._thread.start()

    def follow_selected_unit(self) -> str:
        address, rawcode = self._backend.selected_unit()
        self._activate_follow_target(address)
        return rawcode

    def follow_player_hero(self, player_slot: int, hero_rawcode: str) -> str:
        address, rawcode = self._backend.find_player_hero(player_slot, hero_rawcode, self._stop)
        self._activate_follow_target(address)
        return rawcode

    def resolve_player_hero(self, player_slot: int, hero_rawcode: str) -> tuple[int, str]:
        return self._backend.find_player_hero(player_slot, hero_rawcode, self._stop)

    def follow_resolved_unit(self, address: int) -> None:
        self._backend.unit_camera_position(address)
        self._activate_follow_target(address)

    def _activate_follow_target(self, address: int) -> None:
        self._clear_transition()
        with self._follow_lock:
            self._follow_unit = address
        with self._drone_lock:
            if self._drone_enabled:
                self._drone_target_lock = True
        self._input_router.set_follow_active(True)

    def clear_follow(self) -> None:
        with self._follow_lock:
            self._follow_unit = None
            self._smart_follow = False
        with self._drone_lock:
            self._drone_target_lock = False
            self._orbit_enabled = False
            self._orbit_manual_dolly = False
            self._orbit_director.deactivate()
            drone_enabled = self._drone_enabled
        self._input_router.set_follow_active(drone_enabled)

    def toggle_smart_follow(self) -> bool:
        with self._follow_lock:
            if self._follow_unit is None:
                raise SeekBackendError('Сначала включи Follow для героя.')
            self._smart_follow = not self._smart_follow
            return self._smart_follow

    def toggle_transition(self, kind: CameraTransitionKind, custom_spec: CameraTransitionSpec | None=None) -> tuple[str, bool]:
        if self.drone_enabled:
            self.toggle_drone()
        with self._transition_lock:
            return_state = self._transition_return_state
            return_kind = self._transition_kind
            return_label = self._transition_label
            return_duration = self._transition_duration
        if return_state is not None and return_kind == kind and (return_label is not None):
            with self._motion_lock:
                current = self._native.ping()
                command = CameraTransitionCommand(subject_x=return_state.target_x, subject_y=return_state.target_y, distance_delta=return_state.distance - current.distance, pitch_delta=return_state.pitch - current.pitch, z_offset_delta=return_state.z_offset - current.z_offset, duration_seconds=max(return_duration, 0.3), target_response=4.5)
                self._native.begin_transition(command)
            with self._transition_lock:
                self._transition = ActiveCameraTransition(kind=kind, subject_address=None, subject_label=return_label, deadline=time.perf_counter() + command.duration_seconds)
                self._transition_return_state = None
                self._transition_kind = None
                self._transition_label = None
                self._transition_duration = 0.0
            return (return_label, False)
        if kind == CameraTransitionKind.CUSTOM and custom_spec is None:
            raise ValueError('Custom transition settings are missing')
        spec = custom_spec if custom_spec is not None else CAMERA_TRANSITION_PRESETS[kind]
        self.clear_follow()
        with self._motion_lock:
            current = self._backend.camera_state()
            self._native.sync_pose(current)
            if spec.track_selected:
                address, subject_label = self._backend.selected_unit()
                subject_x, subject_y = self._backend.unit_camera_position(address)
            else:
                address = None
                subject_label = kind.value
                subject_x = current.target_x
                subject_y = current.target_y
            command = camera_transition_command(current, self._safety, subject_x, subject_y, spec)
            self._native.begin_transition(command)
            with self._transition_lock:
                self._transition = ActiveCameraTransition(kind=kind, subject_address=address, subject_label=subject_label, deadline=time.perf_counter() + command.duration_seconds)
                self._transition_return_state = current
                self._transition_kind = kind
                self._transition_label = subject_label
                self._transition_duration = command.duration_seconds
        return (subject_label, True)

    def _transition_session(self) -> ActiveCameraTransition | None:
        with self._transition_lock:
            return self._transition

    def _clear_transition(self) -> None:
        with self._transition_lock:
            self._transition = None
            self._transition_return_state = None
            self._transition_kind = None
            self._transition_label = None
            self._transition_duration = 0.0

    def _finish_transition_tracking(self) -> None:
        with self._transition_lock:
            self._transition = None

    def reset_view(self) -> None:
        if self.drone_enabled:
            self.toggle_drone()
        self.clear_follow()
        self._clear_transition()
        self._reset_requested.set()

    def _follow_address(self) -> int | None:
        with self._follow_lock:
            return self._follow_unit

    def _follow_mode(self) -> tuple[int | None, bool]:
        with self._follow_lock:
            return (self._follow_unit, self._smart_follow)

    def _drone_mode(self) -> tuple[bool, bool, bool]:
        with self._drone_lock:
            return (self._drone_enabled, self._drone_target_lock, self._orbit_enabled)

    def _orbit_inputs(self, frame: DirectorFrame, manual_dolly: float) -> tuple[float, float]:
        with self._drone_lock:
            if not self._orbit_enabled:
                return (0.0, manual_dolly)
            if abs(manual_dolly) > 1e-06:
                self._orbit_director.rebase(frame.distance)
                self._orbit_manual_dolly = True
            elif self._orbit_manual_dolly:
                self._orbit_director.rebase(frame.distance)
                self._orbit_manual_dolly = False
            output = self._orbit_director.update(frame)
            yaw_speed = self._drone_settings.yaw_speed
            dolly_speed = self._drone_settings.dolly_speed
        yaw_input = min(max(output.yaw_velocity / yaw_speed, -1.0), 1.0)
        if abs(manual_dolly) > 1e-06:
            return (yaw_input, manual_dolly)
        return (yaw_input, orbit_dolly_axis(frame.distance, output.distance, output.distance_velocity, dolly_speed))

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join()
        self._thread = None
        with self._drone_lock:
            self._drone_enabled = False
            self._drone_target_lock = False
            self._orbit_enabled = False
            self._orbit_manual_dolly = False
            self._orbit_director.deactivate()
        self._input_router.set_follow_active(False)
        self._native.close()

    def _pressed(self, key: int) -> bool:
        return self._input_router.is_pressed(key)

    def _warcraft_is_foreground(self) -> bool:
        window = self._user32.GetForegroundWindow()
        if not window:
            return False
        pid = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
        return int(pid.value) == self._backend.process_id

    @staticmethod
    def _approach(value: float, target: float, response: float, delta_seconds: float) -> float:
        blend = 1.0 - math.exp(-response * delta_seconds)
        return value + (target - value) * blend

    @staticmethod
    def _bounded_pitch(value: float, delta: float, home_pitch: float) -> float:
        half_turn = math.pi / 2.0
        return min(max(value + delta, home_pitch - half_turn), home_pitch + half_turn)

    def _run(self) -> None:
        try:
            state = self._backend.camera_state()
            forward_velocity = 0.0
            right_velocity = 0.0
            rotation_velocity = 0.0
            pitch_velocity = 0.0
            zoom_velocity = 0.0
            vertical_velocity = 0.0
            idle = True
            follow_offset_x = 0.0
            follow_offset_y = 0.0
            follow_position: tuple[float, float] | None = None
            previous_follow_position: tuple[float, float] | None = None
            previous_follow_time = 0.0
            follow_velocity_x = 0.0
            follow_velocity_y = 0.0
            smart_director = SmartFollowDirector()
            smart_follow_was_active = False
            tracked_follow_address: int | None = None
            follow_transition_response = 5.0
            follow_transition_until = 0.0
            last_follow_read = 0.0
            last_tick = time.perf_counter()
            last_state_emit = last_tick
            last_native_ping = last_tick
            was_focused = False
            while not self._stop.is_set():
                with self._settings_lock:
                    settings = self._settings
                interval = 1.0 / settings.update_hz
                now = time.perf_counter()
                delta = min(max(now - last_tick, 0.0001), 0.05)
                last_tick = now
                if self._reset_requested.is_set():
                    self.clear_follow()
                    follow_position = None
                    follow_offset_x = follow_offset_y = 0.0
                    forward_velocity = right_velocity = 0.0
                    rotation_velocity = pitch_velocity = 0.0
                    zoom_velocity = vertical_velocity = 0.0
                    with self._motion_lock:
                        current = self._native.ping()
                        state = standard_view_at(self._home_state, current)
                        self._native.set_target(state, 0.0)
                    self._reset_requested.clear()
                    idle = True
                    if self._on_state is not None:
                        self._on_state(state)
                        last_state_emit = now
                focused = self._warcraft_is_foreground()
                regained_focus = focused and (not was_focused)
                was_focused = focused
                if focused:
                    forward_input = float(self._pressed(self.VK_UP)) - float(self._pressed(self.VK_DOWN))
                    right_input = float(self._pressed(self.VK_RIGHT)) - float(self._pressed(self.VK_LEFT))
                    rotation_input = float(self._pressed(self.VK_DELETE)) - float(self._pressed(self.VK_INSERT))
                    pitch_input = float(self._pressed(self.VK_HOME)) - float(self._pressed(self.VK_END))
                    zoom_input = float(self._pressed(self.VK_NEXT)) - float(self._pressed(self.VK_PRIOR))
                    vertical_input = float(self._input_router.is_action_pressed('drone_height_up')) - float(self._input_router.is_action_pressed('drone_height_down'))
                else:
                    forward_input = right_input = 0.0
                    rotation_input = pitch_input = zoom_input = 0.0
                    vertical_input = 0.0
                has_input = any((value for value in (forward_input, right_input, rotation_input, pitch_input, zoom_input, vertical_input)))
                follow_address, smart_follow = self._follow_mode()
                drone_enabled, drone_target_lock, orbit_enabled = self._drone_mode()
                taking_control = not drone_enabled and idle and focused and (has_input or (follow_address is not None and (tracked_follow_address is None or regained_focus)))
                if taking_control:
                    state = self._backend.camera_state()
                    with self._motion_lock:
                        self._native.sync_pose(state)
                    idle = False
                if follow_address != tracked_follow_address:
                    switching_heroes = tracked_follow_address is not None and follow_address is not None
                    follow_position = None
                    previous_follow_position = None
                    previous_follow_time = 0.0
                    follow_velocity_x = follow_velocity_y = 0.0
                    smart_director.reset(state.yaw)
                    follow_transition_response = 5.0
                    follow_transition_until = 0.0
                    if switching_heroes and follow_address is not None:
                        try:
                            follow_position = self._backend.unit_camera_position(follow_address)
                        except SeekBackendError:
                            follow_position = None
                        if follow_position is not None:
                            switch_distance = math.hypot(follow_position[0] - state.target_x, follow_position[1] - state.target_y)
                            transition = hero_switch_transition(switch_distance)
                            follow_transition_response = transition.response
                            follow_transition_until = now + transition.duration_seconds
                    tracked_follow_address = follow_address
                if smart_follow != smart_follow_was_active:
                    smart_director.reset(state.yaw)
                    smart_follow_was_active = smart_follow
                if drone_enabled:
                    if drone_target_lock and follow_address is not None and (now - last_follow_read >= 1.0 / 60.0):
                        try:
                            next_follow_position = self._backend.unit_camera_position(follow_address)
                            if previous_follow_position is not None and previous_follow_time > 0.0:
                                follow_delta = max(now - previous_follow_time, 0.001)
                                measured_x = (next_follow_position[0] - previous_follow_position[0]) / follow_delta
                                measured_y = (next_follow_position[1] - previous_follow_position[1]) / follow_delta
                                follow_velocity_x = self._approach(follow_velocity_x, measured_x, 8.0, follow_delta)
                                follow_velocity_y = self._approach(follow_velocity_y, measured_y, 8.0, follow_delta)
                            previous_follow_position = next_follow_position
                            previous_follow_time = now
                            follow_position = next_follow_position
                        except SeekBackendError as exc:
                            self.clear_follow()
                            follow_address = None
                            follow_position = None
                            drone_target_lock = False
                            if self._on_follow_lost is not None:
                                self._on_follow_lost(str(exc))
                        last_follow_read = now
                    if drone_target_lock and follow_position is not None:
                        subject_x, subject_y = follow_position
                        subject_velocity_x = follow_velocity_x
                        subject_velocity_y = follow_velocity_y
                    else:
                        subject_x = state.target_x
                        subject_y = state.target_y
                        subject_velocity_x = 0.0
                        subject_velocity_y = 0.0
                    drone_rotation_input = rotation_input
                    drone_dolly_input = zoom_input
                    if orbit_enabled and drone_target_lock and (follow_position is not None):
                        orbit_yaw_input, drone_dolly_input = self._orbit_inputs(DirectorFrame(yaw=state.yaw, pitch=state.pitch, distance=state.distance, z_offset=state.z_offset, subject_x=subject_x, subject_y=subject_y, velocity_x=subject_velocity_x, velocity_y=subject_velocity_y, delta_seconds=delta), zoom_input)
                        drone_rotation_input = min(max(rotation_input + orbit_yaw_input, -1.0), 1.0)
                    with self._motion_lock:
                        state = self._native.set_drone_input(DroneInput(forward=forward_input, strafe=right_input, lift=vertical_input, yaw=drone_rotation_input, pitch=pitch_input, dolly=drone_dolly_input, subject_x=subject_x, subject_y=subject_y, subject_velocity_x=subject_velocity_x, subject_velocity_y=subject_velocity_y, target_lock=drone_target_lock and follow_position is not None))
                    idle = False
                    if self._on_state is not None and now - last_state_emit >= 0.2:
                        self._on_state(state)
                        last_state_emit = now
                    elapsed = time.perf_counter() - now
                    self._stop.wait(max(interval - elapsed, 0.001))
                    continue
                transition = self._transition_session()
                if transition is None and has_input:
                    with self._transition_lock:
                        return_pending = self._transition_return_state is not None
                    if return_pending:
                        self._clear_transition()
                if transition is not None:
                    if has_input:
                        with self._motion_lock:
                            state = self._native.ping()
                            self._native.set_target(state, 0.0)
                        self._clear_transition()
                        idle = False
                    elif now < transition.deadline:
                        transition_ok = True
                        if transition.subject_address is not None:
                            try:
                                subject_x, subject_y = self._backend.unit_camera_position(transition.subject_address)
                                with self._motion_lock:
                                    self._native.update_subject(subject_x, subject_y)
                            except SeekBackendError as exc:
                                with self._motion_lock:
                                    state = self._native.ping()
                                    self._native.set_target(state, 0.0)
                                self._clear_transition()
                                transition_ok = False
                                if self._on_follow_lost is not None:
                                    self._on_follow_lost(str(exc))
                        if transition_ok:
                            elapsed = time.perf_counter() - now
                            self._stop.wait(max(interval - elapsed, 0.001))
                            continue
                    else:
                        with self._motion_lock:
                            state = self._native.ping()
                        self._finish_transition_tracking()
                        idle = True
                response = settings.smoothing
                forward_velocity = self._approach(forward_velocity, forward_input * settings.move_speed, response, delta)
                right_velocity = self._approach(right_velocity, right_input * settings.move_speed, response, delta)
                rotation_velocity = self._approach(rotation_velocity, rotation_input * settings.rotation_speed, response, delta)
                pitch_velocity = self._approach(pitch_velocity, pitch_input * settings.rotation_speed, response, delta)
                zoom_velocity = self._approach(zoom_velocity, zoom_input * settings.zoom_speed, response, delta)
                vertical_velocity = self._approach(vertical_velocity, vertical_input * settings.lift_speed, response, delta)
                moving = max(abs(forward_velocity) / settings.move_speed, abs(right_velocity) / settings.move_speed, abs(rotation_velocity) / settings.rotation_speed, abs(pitch_velocity) / settings.rotation_speed, abs(zoom_velocity) / settings.zoom_speed, abs(vertical_velocity) / settings.lift_speed)
                if not focused:
                    forward_velocity = right_velocity = 0.0
                    rotation_velocity = zoom_velocity = 0.0
                    pitch_velocity = vertical_velocity = 0.0
                    idle = True
                elif moving > 0.001 or follow_address is not None:
                    yaw = math.remainder(state.yaw + rotation_velocity * delta, 2 * math.pi)
                    pitch = min(max(state.pitch + pitch_velocity * delta, self._safety.pitch_min), self._safety.pitch_max)
                    forward = forward_velocity * delta
                    right = right_velocity * delta
                    pan_x = math.cos(yaw) * forward + math.cos(yaw - math.pi / 2) * right
                    pan_y = math.sin(yaw) * forward + math.sin(yaw - math.pi / 2) * right
                    if follow_address is not None:
                        if now - last_follow_read >= 1.0 / 60.0:
                            try:
                                next_follow_position = self._backend.unit_camera_position(follow_address)
                                if previous_follow_position is not None and previous_follow_time > 0.0:
                                    follow_delta = max(now - previous_follow_time, 0.001)
                                    measured_x = (next_follow_position[0] - previous_follow_position[0]) / follow_delta
                                    measured_y = (next_follow_position[1] - previous_follow_position[1]) / follow_delta
                                    follow_velocity_x = self._approach(follow_velocity_x, measured_x, 8.0, follow_delta)
                                    follow_velocity_y = self._approach(follow_velocity_y, measured_y, 8.0, follow_delta)
                                previous_follow_position = next_follow_position
                                previous_follow_time = now
                                follow_position = next_follow_position
                            except SeekBackendError as exc:
                                self.clear_follow()
                                follow_address = None
                                follow_position = None
                                if self._on_follow_lost is not None:
                                    self._on_follow_lost(str(exc))
                            last_follow_read = now
                        if follow_address is None:
                            target_x = state.target_x + pan_x
                            target_y = state.target_y + pan_y
                        else:
                            follow_offset_x += pan_x
                            follow_offset_y += pan_y
                            if not forward_input and (not right_input):
                                follow_offset_x = self._approach(follow_offset_x, 0.0, 0.7, delta)
                                follow_offset_y = self._approach(follow_offset_y, 0.0, 0.7, delta)
                            if follow_position is None:
                                target_x = state.target_x
                                target_y = state.target_y
                            else:
                                look_ahead_x = 0.0
                                look_ahead_y = 0.0
                                if smart_follow:
                                    director_output = smart_director.update(DirectorFrame(yaw=state.yaw, pitch=state.pitch, distance=state.distance, z_offset=state.z_offset, subject_x=follow_position[0], subject_y=follow_position[1], velocity_x=follow_velocity_x, velocity_y=follow_velocity_y, delta_seconds=delta))
                                    yaw = director_output.yaw
                                    look_ahead_x = director_output.target_offset_x
                                    look_ahead_y = director_output.target_offset_y
                                target_x = self._approach(state.target_x, follow_position[0] + look_ahead_x + follow_offset_x, follow_transition_response if now < follow_transition_until else settings.follow_smoothing, delta)
                                target_y = self._approach(state.target_y, follow_position[1] + look_ahead_y + follow_offset_y, follow_transition_response if now < follow_transition_until else settings.follow_smoothing, delta)
                    else:
                        follow_position = None
                        previous_follow_position = None
                        previous_follow_time = 0.0
                        follow_velocity_x = follow_velocity_y = 0.0
                        smart_director.reset(state.yaw)
                        follow_offset_x = follow_offset_y = 0.0
                        target_x = state.target_x + pan_x
                        target_y = state.target_y + pan_y
                    distance = min(max(state.distance + zoom_velocity * delta, self._safety.distance_min), self._safety.distance_max)
                    z_offset = min(max(state.z_offset + vertical_velocity * delta, self._safety.z_offset_min), self._safety.z_offset_max)
                    target_x = min(max(target_x, self._safety.target_x_min), self._safety.target_x_max)
                    target_y = min(max(target_y, self._safety.target_y_min), self._safety.target_y_max)
                    state = replace(state, target_x=target_x, target_y=target_y, yaw=yaw, pitch=pitch, distance=distance, z_offset=z_offset)
                    with self._motion_lock:
                        if self._transition_session() is None:
                            self._native.set_target(state, max(settings.smoothing * 2.5, 12.0))
                    if self._on_state is not None and now - last_state_emit >= 0.2:
                        self._on_state(state)
                        last_state_emit = now
                elif not has_input:
                    idle = True
                if now - last_native_ping >= 1.0:
                    with self._motion_lock:
                        self._native.ping()
                    last_native_ping = now
                elapsed = time.perf_counter() - now
                self._stop.wait(max(interval - elapsed, 0.001))
        except (SeekBackendError, OSError, ValueError) as exc:
            if self._on_error is not None and (not self._stop.is_set()):
                self._on_error(str(exc))
        finally:
            self._input_router.set_follow_active(False)
            self._native.close()
            self._stop.set()
