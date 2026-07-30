from __future__ import annotations
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

class CameraRigMode(str, Enum):
    FREE = 'free'
    FOLLOW = 'follow'
    SMART_FOLLOW = 'smart_follow'
    CINEMATIC = 'cinematic'
    ORBIT = 'orbit'
    DRONE = 'drone'

class CameraTransitionKind(str, Enum):
    DOLLY_OUT = 'dolly_out'
    CRANE_UP = 'crane_up'
    REVEAL = 'reveal'
    PUSH_IN = 'push_in'
    FOCUS_PULL = 'focus_pull'
    CUSTOM = 'custom'

@dataclass(frozen=True)
class CameraTransitionSpec:
    distance_delta: float
    pitch_delta: float
    z_offset_delta: float
    duration_seconds: float
    target_response: float
    track_selected: bool

    def __post_init__(self) -> None:
        values = (self.distance_delta, self.pitch_delta, self.z_offset_delta, self.duration_seconds, self.target_response)
        if not all((math.isfinite(value) for value in values)):
            raise ValueError('Camera transition values must be finite')
        if not 0.3 <= self.duration_seconds <= 10.0:
            raise ValueError('Camera transition duration must be in range 0.3..10')
        if not 0.0 <= self.target_response <= 100.0:
            raise ValueError('Camera transition response must be in range 0..100')
CAMERA_TRANSITION_PRESETS = {CameraTransitionKind.DOLLY_OUT: CameraTransitionSpec(distance_delta=650.0, pitch_delta=0.0, z_offset_delta=0.0, duration_seconds=2.0, target_response=0.0, track_selected=False), CameraTransitionKind.CRANE_UP: CameraTransitionSpec(distance_delta=0.0, pitch_delta=0.0, z_offset_delta=300.0, duration_seconds=2.2, target_response=0.0, track_selected=False), CameraTransitionKind.REVEAL: CameraTransitionSpec(distance_delta=700.0, pitch_delta=-0.05, z_offset_delta=140.0, duration_seconds=2.5, target_response=4.5, track_selected=True), CameraTransitionKind.PUSH_IN: CameraTransitionSpec(distance_delta=-450.0, pitch_delta=0.0, z_offset_delta=0.0, duration_seconds=1.8, target_response=0.0, track_selected=False), CameraTransitionKind.FOCUS_PULL: CameraTransitionSpec(distance_delta=600.0, pitch_delta=0.0, z_offset_delta=0.0, duration_seconds=2.2, target_response=5.0, track_selected=True)}
DEFAULT_CUSTOM_TRANSITION = CameraTransitionSpec(distance_delta=500.0, pitch_delta=math.radians(-3.0), z_offset_delta=100.0, duration_seconds=2.5, target_response=4.5, track_selected=True)

def tune_transition(base: CameraTransitionSpec, strength_percent: float, duration_seconds: float) -> CameraTransitionSpec:
    if not math.isfinite(strength_percent):
        raise ValueError('Transition strength must be finite')
    if not 25.0 <= strength_percent <= 200.0:
        raise ValueError('Transition strength must be in range 25..200')
    scale = strength_percent / 100.0
    return CameraTransitionSpec(distance_delta=base.distance_delta * scale, pitch_delta=base.pitch_delta * scale, z_offset_delta=base.z_offset_delta * scale, duration_seconds=duration_seconds, target_response=base.target_response, track_selected=base.track_selected)

@dataclass(frozen=True)
class DirectorOutput:
    yaw: float
    target_offset_x: float = 0.0
    target_offset_y: float = 0.0
    yaw_velocity: float = 0.0
    pitch: float | None = None
    distance: float | None = None
    distance_velocity: float = 0.0
    z_offset: float | None = None

@dataclass(frozen=True)
class DirectorFrame:
    yaw: float
    pitch: float
    distance: float
    z_offset: float
    subject_x: float
    subject_y: float
    velocity_x: float
    velocity_y: float
    delta_seconds: float

class CameraDirector(Protocol):
    mode: CameraRigMode

    def reset(self, yaw: float) -> None:
        ...

    def update(self, frame: DirectorFrame) -> DirectorOutput:
        ...

@dataclass
class OrbitDirector:
    mode = CameraRigMode.ORBIT
    speed_degrees: float = 18.0
    direction: int = 1
    transition_seconds: float = 2.0
    ring_offsets: tuple[float, float, float] = (-450.0, 0.0, 750.0)
    ring_index: int = field(default=1, init=False)
    _base_distance: float | None = field(default=None, init=False)
    _distance_min: float = field(default=0.0, init=False)
    _distance_max: float = field(default=0.0, init=False)
    _transition_start: float = field(default=0.0, init=False)
    _transition_target: float = field(default=0.0, init=False)
    _transition_elapsed: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self.set_speed(self.speed_degrees)
        if self.direction not in (-1, 1):
            raise ValueError('Orbit direction must be -1 or 1')
        if not math.isfinite(self.transition_seconds) or not 0.3 <= self.transition_seconds <= 10.0:
            raise ValueError('Orbit transition must be in range 0.3..10 seconds')
        if len(self.ring_offsets) != 3 or any((not math.isfinite(value) for value in self.ring_offsets)) or (not self.ring_offsets[0] < self.ring_offsets[1] < self.ring_offsets[2]) or (abs(self.ring_offsets[1]) > 1e-09):
            raise ValueError('Orbit rings must be three ascending offsets around zero')

    def reset(self, yaw: float) -> None:
        if not math.isfinite(yaw):
            raise ValueError('Orbit yaw must be finite')

    def set_speed(self, speed_degrees: float) -> None:
        if not math.isfinite(speed_degrees) or not 2.0 <= speed_degrees <= 90.0:
            raise ValueError('Orbit speed must be in range 2..90')
        self.speed_degrees = speed_degrees

    def activate(self, distance: float, distance_min: float, distance_max: float) -> None:
        if not all((math.isfinite(value) for value in (distance, distance_min, distance_max))) or distance_min >= distance_max:
            raise ValueError('Orbit distance limits are invalid')
        self._distance_min = distance_min
        self._distance_max = distance_max
        self._base_distance = min(max(distance, distance_min), distance_max)
        self.ring_index = 1
        self._transition_start = self._base_distance
        self._transition_target = self._base_distance
        self._transition_elapsed = self.transition_seconds

    def deactivate(self) -> None:
        self._base_distance = None
        self.ring_index = 1
        self._transition_start = 0.0
        self._transition_target = 0.0
        self._transition_elapsed = 0.0

    def rebase(self, distance: float) -> None:
        if self._base_distance is None or not math.isfinite(distance):
            raise ValueError('Orbit rings are not active')
        self.activate(distance, self._distance_min, self._distance_max)

    def shift_ring(self, step: int, distance: float) -> int:
        if self._base_distance is None:
            raise ValueError('Orbit rings are not active')
        if step not in (-1, 1):
            raise ValueError('Orbit ring step must be -1 or 1')
        if not math.isfinite(distance):
            raise ValueError('Orbit distance must be finite')
        next_index = min(max(self.ring_index + step, 0), 2)
        if next_index == self.ring_index:
            return self.ring_index
        self.ring_index = next_index
        self._transition_start = min(max(distance, self._distance_min), self._distance_max)
        self._transition_target = self.ring_distance(next_index)
        self._transition_elapsed = 0.0
        return self.ring_index

    def ring_distance(self, ring_index: int | None=None) -> float:
        if self._base_distance is None:
            raise ValueError('Orbit rings are not active')
        selected = self.ring_index if ring_index is None else ring_index
        if not 0 <= selected < len(self.ring_offsets):
            raise ValueError('Orbit ring index must be in range 0..2')
        return min(max(self._base_distance + self.ring_offsets[selected], self._distance_min), self._distance_max)

    def reverse(self) -> int:
        self.direction *= -1
        return self.direction

    def update(self, frame: DirectorFrame) -> DirectorOutput:
        distance: float | None = None
        distance_velocity = 0.0
        if self._base_distance is not None:
            self._transition_elapsed = min(self._transition_elapsed + max(frame.delta_seconds, 0.0), self.transition_seconds)
            progress = self._transition_elapsed / self.transition_seconds
            eased = progress * progress * progress * (progress * (progress * 6.0 - 15.0) + 10.0)
            distance_delta = self._transition_target - self._transition_start
            distance = self._transition_start + distance_delta * eased
            if progress < 1.0:
                eased_derivative = 30.0 * progress * progress * (progress - 1.0) * (progress - 1.0)
                distance_velocity = distance_delta * eased_derivative / self.transition_seconds
        return DirectorOutput(yaw=frame.yaw, yaw_velocity=math.radians(self.speed_degrees) * self.direction, distance=distance, distance_velocity=distance_velocity)

@dataclass(frozen=True)
class HeroSwitchTransition:
    response: float
    duration_seconds: float

def hero_switch_transition(distance: float) -> HeroSwitchTransition:
    if distance <= 96.0:
        return HeroSwitchTransition(response=1.8, duration_seconds=1.6)
    return HeroSwitchTransition(response=5.0, duration_seconds=0.0)

@dataclass
class SmartFollowDirector:
    mode = CameraRigMode.SMART_FOLLOW
    yaw: float | None = None
    desired_yaw: float | None = None
    candidate_yaw: float | None = None
    candidate_origin_x: float | None = None
    candidate_origin_y: float | None = None
    candidate_age: float = 0.0
    candidate_idle_age: float = 0.0
    last_subject_x: float | None = None
    last_subject_y: float | None = None
    look_ahead: float = 0.0

    def reset(self, yaw: float) -> None:
        self.yaw = yaw
        self.desired_yaw = yaw
        self._reset_candidate()
        self.last_subject_x = None
        self.last_subject_y = None
        self.look_ahead = 0.0

    def _reset_candidate(self) -> None:
        self.candidate_yaw = None
        self.candidate_origin_x = None
        self.candidate_origin_y = None
        self.candidate_age = 0.0
        self.candidate_idle_age = 0.0

    def update(self, frame: DirectorFrame) -> DirectorOutput:
        base_yaw = frame.yaw
        velocity_x = frame.velocity_x
        velocity_y = frame.velocity_y
        delta_seconds = frame.delta_seconds
        if self.yaw is None:
            self.reset(base_yaw)
        previous_subject_x = frame.subject_x if self.last_subject_x is None else self.last_subject_x
        previous_subject_y = frame.subject_y if self.last_subject_y is None else self.last_subject_y
        self.last_subject_x = frame.subject_x
        self.last_subject_y = frame.subject_y
        speed = math.hypot(velocity_x, velocity_y)
        movement_yaw: float | None = None
        target_yaw = self.yaw if self.desired_yaw is None else self.desired_yaw
        if speed >= 0.6:
            movement_yaw = math.atan2(velocity_y, velocity_x)
            turn = abs(math.remainder(movement_yaw - target_yaw, 2 * math.pi))
            if turn < math.radians(12.0):
                self._reset_candidate()
            else:
                if self.candidate_origin_x is None:
                    self.candidate_origin_x = previous_subject_x
                    self.candidate_origin_y = previous_subject_y
                self.candidate_age += delta_seconds
                self.candidate_idle_age = 0.0
                displacement_x = frame.subject_x - self.candidate_origin_x
                displacement_y = frame.subject_y - self.candidate_origin_y
                displacement = math.hypot(displacement_x, displacement_y)
                if displacement >= 0.25:
                    self.candidate_yaw = math.atan2(displacement_y, displacement_x)
                candidate_turn = 0.0 if self.candidate_yaw is None else abs(math.remainder(self.candidate_yaw - target_yaw, 2 * math.pi))
                if self.candidate_yaw is not None and candidate_turn < math.radians(12.0):
                    self._reset_candidate()
                else:
                    if candidate_turn >= math.radians(120.0):
                        required_age = 2.0
                        required_distance = 24.0
                    elif candidate_turn >= math.radians(65.0):
                        required_age = 1.0
                        required_distance = 12.0
                    else:
                        required_age = 0.5
                        required_distance = 5.0
                    if self.candidate_yaw is not None and (self.candidate_age >= required_age and displacement >= required_distance or (self.candidate_age >= 3.0 and displacement >= 12.0)):
                        target_yaw = self.candidate_yaw
                        self.desired_yaw = target_yaw
                        self._reset_candidate()
        else:
            self.candidate_idle_age += delta_seconds
            if self.candidate_idle_age >= 0.75:
                self._reset_candidate()
        self.yaw += math.remainder(target_yaw - self.yaw, 2 * math.pi) * (1.0 - math.exp(-1.35 * delta_seconds))
        aligned = movement_yaw is not None and abs(math.remainder(movement_yaw - self.yaw, 2 * math.pi)) <= math.radians(45.0)
        desired_look_ahead = min(max((speed - 1.5) * 0.25, 0.0), 2.5) if aligned else 0.0
        self.look_ahead += (desired_look_ahead - self.look_ahead) * (1.0 - math.exp(-1.6 * delta_seconds))
        return DirectorOutput(yaw=self.yaw, target_offset_x=math.cos(self.yaw) * self.look_ahead, target_offset_y=math.sin(self.yaw) * self.look_ahead)
