from __future__ import annotations
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from .native_runtime import native_binary_candidates
from .replay_models import *
from .creep_models import RecordedCreepCheckpoint, RecordedCreepTimeline, RecordedCreepTimelineBundle
from .moments import ReplayMoment, ReplayMomentKind

class ReplayParseError(ValueError):
    pass

def _native_engine_candidates() -> list[Path]:
    return native_binary_candidates('replaylab_replay_inspect.exe', environment_variable='REPLAYLAB_REPLAY_ENGINE', build_subdirectory='replay_core')

def find_native_replay_engine() -> Path:
    for candidate in _native_engine_candidates():
        if candidate.is_file():
            return candidate.resolve()
    raise ReplayParseError('Native Replay Engine was not found. Reinstall ReplayLab.')

def _items(values: list[dict[str, Any]], constructor: type[Any]) -> list[Any]:
    return [constructor(**value) for value in values]

def _recorded_creep_bundle(value: dict[str, Any] | None) -> RecordedCreepTimelineBundle | None:
    if value is None:
        return None
    timelines: dict[int, RecordedCreepTimeline] = {}
    for raw_slot, raw_timeline in value.get('timelines', {}).items():
        slot = int(raw_slot)
        checkpoints = tuple((RecordedCreepCheckpoint(**{**point, 'sources': tuple(point.get('sources', []))}) for point in raw_timeline.get('checkpoints', [])))
        timelines[slot] = RecordedCreepTimeline(player_slot=raw_timeline.get('player_slot', slot), checkpoints=checkpoints, sources=tuple(raw_timeline.get('sources', [])), max_gap_ms=raw_timeline.get('max_gap_ms'), issues=tuple(raw_timeline.get('issues', [])))
    return RecordedCreepTimelineBundle(schema_version=value.get('schema_version', 1), timelines=timelines)

def _report_from_native(payload: dict[str, Any]) -> ReplayReport:
    snapshots = [DotaStatsSnapshot(**{**value, 'players': _items(value.get('players', []), DotaPlayerSnapshot)}) for value in payload.get('dota_stats_snapshots', [])]
    invokes = [InvokerInvokeEvent(**{**value, 'spell_rawcodes': tuple(value.get('spell_rawcodes', []))}) for value in payload.get('invoker_invokes', [])]
    return ReplayReport(source_file=payload['source_file'], header=ReplayHeader(**payload['header']), game_name=payload.get('game_name', ''), map_path=payload.get('map_path', ''), map_creator=payload.get('map_creator', ''), player_count_declared=payload.get('player_count_declared', 0), game_type=payload.get('game_type', 0), language_id=payload.get('language_id', 0), random_seed=payload.get('random_seed'), select_mode=payload.get('select_mode'), start_spot_count=payload.get('start_spot_count'), players=_items(payload.get('players', []), Player), slots=_items(payload.get('slots', []), Slot), chats=_items(payload.get('chats', []), ChatMessage), leaves=_items(payload.get('leaves', []), LeaveEvent), command_packets=_items(payload.get('command_packets', []), CommandPacket), string_candidates=_items(payload.get('string_candidates', []), StringCandidate), gamecache_syncs=_items(payload.get('gamecache_syncs', []), GameCacheSync), game_start_ms=payload.get('game_start_ms'), dota_players=_items(payload.get('dota_players', []), DotaPlayer), dota_stats_snapshots=snapshots, recorded_creep_timelines=_recorded_creep_bundle(payload.get('recorded_creep_timelines')), item_timings=_items(payload.get('item_timings', []), ItemTiming), item_orders=_items(payload.get('item_orders', []), ItemOrder), skill_learns=_items(payload.get('skill_learns', []), SkillLearnEvent), invoker_invokes=invokes, kills=_items(payload.get('kills', []), KillEvent), multi_kills=_items(payload.get('multi_kills', []), MultiKillEvent), moments=[ReplayMoment(**{**value, 'kind': ReplayMomentKind(value['kind']), 'victim_ids': tuple(value.get('victim_ids', [])), 'victim_names': tuple(value.get('victim_names', []))}) for value in payload.get('moments', [])], block_counts=payload.get('block_counts', {}), decompressed_bytes=payload.get('decompressed_bytes', 0), parsed_timeline_ms=payload.get('parsed_timeline_ms', 0), warnings=list(payload.get('warnings', [])), action_decode_issues=payload.get('action_decode_issues', {}), command_packet_count=payload.get('command_packet_count', len(payload.get('command_packets', []))), gamecache_sync_count=payload.get('gamecache_sync_count', len(payload.get('gamecache_syncs', []))), string_candidate_count=payload.get('string_candidate_count', len(payload.get('string_candidates', []))))

def parse_replay(path: str | Path, *, include_diagnostics: bool=False) -> ReplayReport:
    replay_path = Path(path)
    engine = find_native_replay_engine()
    profiles = Path(__file__).with_name('profiles')
    command = [str(engine), '--json', '--profiles', str(profiles), str(replay_path)]
    if include_diagnostics:
        command.insert(2, '--include-diagnostics')
    creation_flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, creationflags=creation_flags if os.name == 'nt' else 0)
    except OSError as exc:
        raise ReplayParseError(f'Native Replay Engine could not start: {exc}') from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode('utf-8', 'replace').strip()
        raise ReplayParseError(detail or 'Native Replay Engine rejected the replay')
    try:
        payload = json.loads(completed.stdout.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayParseError('Native Replay Engine returned an invalid presentation response') from exc
    if not isinstance(payload, dict):
        raise ReplayParseError('Native Replay Engine response has the wrong shape')
    return _report_from_native(payload)

def invoker_spells_at(events: list[InvokerInvokeEvent], player_slot: int, replay_time_ms: int) -> tuple[str, ...]:
    result: tuple[str, ...] = ()
    for event in events:
        if event.replay_time_ms > replay_time_ms:
            break
        if event.player_slot == player_slot:
            result = event.spell_rawcodes
    return result
