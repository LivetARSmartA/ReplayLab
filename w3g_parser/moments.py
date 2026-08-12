from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .replay_models import ReplayReport

class ReplayMomentKind(str, Enum):
    KILL = 'kill'
    FIRST_BLOOD = 'first_blood'
    MULTI_KILL = 'multi_kill'

@dataclass(frozen=True)
class ReplayMoment:
    replay_time_ms: int
    game_time_ms: int
    kind: ReplayMomentKind
    label: str
    killer_id: int
    killer_name: str
    killer_hero_rawcode: str | None
    killer_hero_name: str | None
    victim_ids: tuple[int, ...]
    victim_names: tuple[str, ...]
    severity: int

def build_replay_moments(report: ReplayReport) -> list[ReplayMoment]:
    return list(report.moments)
