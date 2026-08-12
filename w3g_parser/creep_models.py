from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class RecordedCreepCheckpoint:
    player_slot: int
    game_time_ms: int
    creep_kills: int
    creep_denies: int
    neutral_kills: int
    sources: tuple[str, ...]
    confidence: str = 'exact-at-checkpoint'

@dataclass(frozen=True)
class RecordedCreepTimeline:
    player_slot: int
    checkpoints: tuple[RecordedCreepCheckpoint, ...]
    sources: tuple[str, ...]
    max_gap_ms: int | None
    issues: tuple[str, ...]

    @property
    def is_valid(self) -> bool:
        return bool(self.checkpoints) and (not self.issues)

@dataclass(frozen=True)
class RecordedCreepTimelineBundle:
    schema_version: int
    timelines: dict[int, RecordedCreepTimeline]

    def for_player(self, player_slot: int) -> RecordedCreepTimeline | None:
        return self.timelines.get(player_slot)
