from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from .parser import KillEvent, ReplayReport

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

def _is_player_kill(event: KillEvent) -> bool:
    return event.killer_id != event.victim_id and event.killer_hero_rawcode is not None and (event.victim_hero_rawcode is not None)

def build_replay_moments(report: ReplayReport) -> list[ReplayMoment]:
    visible_kills = [event for event in report.kills if _is_player_kill(event)]
    first_blood = visible_kills[0] if visible_kills else None
    multi_by_finish = {(event.replay_time_ms, event.killer_id): event for event in report.multi_kills}
    moments: list[ReplayMoment] = []
    for event in visible_kills:
        multi = multi_by_finish.get((event.replay_time_ms, event.killer_id))
        if event is first_blood:
            kind = ReplayMomentKind.FIRST_BLOOD
            label = 'First Blood'
            victim_ids = (event.victim_id,)
            victim_names = (event.victim_name,)
            severity = 3
        elif multi is not None:
            kind = ReplayMomentKind.MULTI_KILL
            label = multi.label
            victim_ids = tuple(multi.victim_ids)
            victim_names = tuple(multi.victim_names)
            severity = multi.count
        else:
            kind = ReplayMomentKind.KILL
            label = 'Kill'
            victim_ids = (event.victim_id,)
            victim_names = (event.victim_name,)
            severity = 1
        moments.append(ReplayMoment(replay_time_ms=event.replay_time_ms, game_time_ms=max(event.game_time_ms, 0), kind=kind, label=label, killer_id=event.killer_id, killer_name=event.killer_name, killer_hero_rawcode=event.killer_hero_rawcode, killer_hero_name=event.killer_hero_name, victim_ids=victim_ids, victim_names=victim_names, severity=severity))
    return moments
