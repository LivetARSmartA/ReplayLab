from __future__ import annotations
from dataclasses import dataclass

class SeekBackendError(RuntimeError):
    pass

class SeekCancelled(SeekBackendError):
    pass

@dataclass(frozen=True)
class AttachResult:
    pid: int
    executable: str
    game_dll: str
    replay_block: int
    replay_position_ms: int
    replay_length_ms: int
    build_profile: str = ''
    game_dll_match: str = ''
    game_dll_sha256: str = ''
    attach_duration_ms: float = 0.0
    binary_validation_ms: float = 0.0
    replay_scan_ms: float = 0.0
    validation_cache_hit: bool = False
    replay_scan_strategy: str = ''

@dataclass(frozen=True)
class ProcessAttachResult:
    pid: int
    executable: str
    game_dll: str
    build_profile: str
    game_dll_match: str
    game_dll_sha256: str
    binary_validation_ms: float = 0.0
    validation_cache_hit: bool = False

@dataclass(frozen=True)
class SeekProgress:
    current_replay_time_ms: int
    target_replay_time_ms: int
    speed_value: int
    stage: str = 'cruise'
    effective_speed: float = 0.0
    eta_seconds: float | None = None
    process_cpu_percent: float | None = None
    command_latency_ms: float = 0.0
    first_advance_ms: float | None = None

@dataclass(frozen=True)
class SeekMetrics:
    start_replay_time_ms: int
    final_replay_time_ms: int
    target_replay_time_ms: int
    wall_duration_ms: float
    command_latency_ms: float
    first_advance_ms: float | None
    effective_speed: float
    process_cpu_percent: float | None
    overshoot_ms: int
    profile_key: str
    high_qos_applied: bool

@dataclass(frozen=True)
class CameraState:
    target_x: float
    target_y: float
    distance: float
    yaw: float
    pitch: float
    roll: float
    z_offset: float

@dataclass(frozen=True)
class SeekProfile:
    key: str
    label: str
    maximum_speed: int
    far_poll_seconds: float
    lower_process_priority: bool
    power_mode: str
    high_qos: bool = False
SEEK_PROFILES = {'gentle': SeekProfile(key='gentle', label='Eco · до 16x', maximum_speed=16, far_poll_seconds=0.2, lower_process_priority=True, power_mode='eco'), 'balanced': SeekProfile(key='balanced', label='Balanced · до 32x', maximum_speed=32, far_poll_seconds=0.12, lower_process_priority=True, power_mode='balanced'), 'turbo': SeekProfile(key='turbo', label='Maximum · максимум', maximum_speed=65535, far_poll_seconds=0.05, lower_process_priority=False, power_mode='maximum', high_qos=True)}
