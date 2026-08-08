from __future__ import annotations
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .parser import ReplayReport
RECORDED_CREEP_TIMELINE_SCHEMA_VERSION = 1
RECORDED_CHECKPOINT_CONFIDENCE = 'exact-at-checkpoint'
WARCRAFT_PLAYER_SLOTS = (1, 2, 3, 4, 5, 7, 8, 9, 10, 11)
_PERIODIC_KEY_RE = re.compile('(CSK|CSD|NK)(\\d+)$')
_COMPACT_KEY_RE = re.compile('CK(\\d+)D(\\d+)N(\\d+)$')
_PERIODIC_PREFIXES = ('CSK', 'CSD', 'NK')
_MAX_COUNTER = 100000

@dataclass(frozen=True)
class RecordedCreepCheckpoint:
    player_slot: int
    game_time_ms: int
    creep_kills: int
    creep_denies: int
    neutral_kills: int
    sources: tuple[str, ...]
    confidence: str = RECORDED_CHECKPOINT_CONFIDENCE

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

def _valid_counter(value: object) -> bool:
    return isinstance(value, int) and 0 <= value <= _MAX_COUNTER

def _checkpoint(player_slot: int, game_time_ms: int, creep_kills: object, creep_denies: object, neutral_kills: object, source: str) -> RecordedCreepCheckpoint | None:
    counters = (creep_kills, creep_denies, neutral_kills)
    if player_slot not in WARCRAFT_PLAYER_SLOTS or game_time_ms < 0 or (not all((_valid_counter(value) for value in counters))):
        return None
    return RecordedCreepCheckpoint(player_slot=player_slot, game_time_ms=game_time_ms, creep_kills=int(creep_kills), creep_denies=int(creep_denies), neutral_kills=int(neutral_kills), sources=(source,))

def _periodic_checkpoints(report: ReplayReport) -> list[RecordedCreepCheckpoint]:
    events_by_time: dict[int, dict[str, int]] = defaultdict(dict)
    for event in report.gamecache_syncs:
        if event.cache_name != 'dr.x' or event.mission_key != 'Data':
            continue
        match = _PERIODIC_KEY_RE.fullmatch(event.key)
        if match is None or not _valid_counter(event.value_i32):
            continue
        prefix, raw_slot = match.groups()
        slot = int(raw_slot)
        if slot not in WARCRAFT_PLAYER_SLOTS:
            continue
        events_by_time[event.time_ms][f'{prefix}{slot}'] = event.value_i32
    expected_keys = {f'{prefix}{slot}' for slot in WARCRAFT_PLAYER_SLOTS for prefix in _PERIODIC_PREFIXES}
    game_start_ms = report.game_start_ms or 0
    checkpoints: list[RecordedCreepCheckpoint] = []
    for replay_time_ms, values in sorted(events_by_time.items()):
        if set(values) != expected_keys:
            continue
        game_time_ms = max(replay_time_ms - game_start_ms, 0)
        for slot in WARCRAFT_PLAYER_SLOTS:
            point = _checkpoint(slot, game_time_ms, values[f'CSK{slot}'], values[f'CSD{slot}'], values[f'NK{slot}'], 'periodic-gamecache')
            if point is not None:
                checkpoints.append(point)
    return checkpoints

def _compact_checkpoints(report: ReplayReport) -> list[RecordedCreepCheckpoint]:
    game_start_ms = report.game_start_ms or 0
    checkpoints: list[RecordedCreepCheckpoint] = []
    for event in report.gamecache_syncs:
        if event.cache_name != 'dr.x' or event.mission_key != 'Data':
            continue
        match = _COMPACT_KEY_RE.fullmatch(event.key)
        if match is None:
            continue
        point = _checkpoint(event.value_i32, max(event.time_ms - game_start_ms, 0), *(int(value) for value in match.groups()), 'leave-summary')
        if point is not None:
            checkpoints.append(point)
    return checkpoints

def _json_checkpoints(report: ReplayReport) -> list[RecordedCreepCheckpoint]:
    checkpoints: list[RecordedCreepCheckpoint] = []
    for snapshot in report.dota_stats_snapshots:
        for player in snapshot.players:
            point = _checkpoint(player.slot, snapshot.game_time_ms, player.creep_kills, player.creep_denies, player.neutral_kills, 'game-stats-json' if snapshot.complete else 'game-stats-partial-player')
            if point is not None:
                checkpoints.append(point)
    return checkpoints

def _final_checkpoints(report: ReplayReport) -> list[RecordedCreepCheckpoint]:
    checkpoints: list[RecordedCreepCheckpoint] = []
    for player in report.dota_players:
        if player.creep_stats_source != 'final':
            continue
        point = _checkpoint(player.slot, player.creep_stats_game_time_ms or 0, player.creep_kills, player.creep_denies, player.neutral_kills, 'final-table')
        if point is not None:
            checkpoints.append(point)
    return checkpoints

def _merge_player_checkpoints(player_slot: int, checkpoints: list[RecordedCreepCheckpoint]) -> RecordedCreepTimeline:
    grouped: dict[tuple[int, int, int, int], set[str]] = defaultdict(set)
    values_at_time: dict[int, set[tuple[int, int, int]]] = defaultdict(set)
    for point in checkpoints:
        counters = (point.creep_kills, point.creep_denies, point.neutral_kills)
        grouped[point.game_time_ms, *counters].update(point.sources)
        values_at_time[point.game_time_ms].add(counters)
    issues = [f'conflicting counters at {game_time_ms} ms' for game_time_ms, values in sorted(values_at_time.items()) if len(values) > 1]
    merged = [RecordedCreepCheckpoint(player_slot=player_slot, game_time_ms=game_time_ms, creep_kills=creep_kills, creep_denies=creep_denies, neutral_kills=neutral_kills, sources=tuple(sorted(sources))) for (game_time_ms, creep_kills, creep_denies, neutral_kills), sources in grouped.items()]
    merged.sort(key=lambda point: point.game_time_ms)
    previous: RecordedCreepCheckpoint | None = None
    for point in merged:
        if previous is not None and (point.creep_kills < previous.creep_kills or point.creep_denies < previous.creep_denies or point.neutral_kills < previous.neutral_kills):
            issues.append(f'counter regression at {point.game_time_ms} ms')
        previous = point
    gaps = [current.game_time_ms - previous.game_time_ms for previous, current in zip(merged, merged[1:])]
    sources = tuple(sorted({source for point in merged for source in point.sources}))
    return RecordedCreepTimeline(player_slot=player_slot, checkpoints=tuple(merged), sources=sources, max_gap_ms=max(gaps) if gaps else None, issues=tuple(issues))

def build_recorded_creep_timelines(report: ReplayReport) -> RecordedCreepTimelineBundle:
    by_slot: dict[int, list[RecordedCreepCheckpoint]] = defaultdict(list)
    for point in (*_periodic_checkpoints(report), *_compact_checkpoints(report), *_json_checkpoints(report), *_final_checkpoints(report)):
        by_slot[point.player_slot].append(point)
    timelines = {slot: _merge_player_checkpoints(slot, by_slot.get(slot, [])) for slot in WARCRAFT_PLAYER_SLOTS if by_slot.get(slot)}
    return RecordedCreepTimelineBundle(schema_version=RECORDED_CREEP_TIMELINE_SCHEMA_VERSION, timelines=timelines)
