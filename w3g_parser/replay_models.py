from __future__ import annotations
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from .creep_models import RecordedCreepTimelineBundle
from .moments import ReplayMoment

@dataclass
class ReplayHeader:
    header_size: int
    compressed_size: int
    header_version: int
    decompressed_size: int
    compressed_blocks: int
    product: str
    version: int
    build: int
    flags: int
    duration_ms: int
    checksum: int

@dataclass
class Player:
    player_id: int
    name: str
    record_id: int
    runtime: int | None = None
    race_flags: int | None = None

@dataclass
class Slot:
    player_id: int
    download_status: int
    slot_status: int
    computer: bool
    team: int
    color: int
    race: int
    computer_type: int
    handicap: int

@dataclass
class ChatMessage:
    time_ms: int
    player_id: int
    mode: int | None
    text: str

@dataclass
class LeaveEvent:
    time_ms: int
    player_id: int
    reason: int
    result: int
    unknown: int

@dataclass
class CommandPacket:
    time_ms: int
    player_id: int
    byte_length: int

@dataclass
class StringCandidate:
    time_ms: int
    player_id: int
    source: str
    text: str
    packet_offset: int

@dataclass
class GameCacheSync:
    time_ms: int
    player_id: int
    cache_name: str
    mission_key: str
    key: str
    value_u32: int
    value_i32: int
    value_hex: str
    value_ascii: str | None
    packet_offset: int

@dataclass
class DotaPlayer:
    slot: int
    network_player_id: int
    name: str
    hero_rawcode: str | None
    hero_name: str | None
    side: str | None = None
    won: bool | None = None
    level: int | None = None
    kills: int | None = None
    deaths: int | None = None
    assists: int | None = None
    creep_kills: int | None = None
    creep_denies: int | None = None
    neutral_kills: int | None = None
    creep_stats_source: str | None = None
    creep_stats_game_time_ms: int | None = None
    final_gold: int | None = None
    inventory_value: int | None = None
    tower_kills: int | None = None
    rax_kills: int | None = None
    courier_kills: int | None = None
    left_time_seconds: int | None = None
    final_item_rawcodes: list[str | None] = field(default_factory=list)
    final_item_names: list[str | None] = field(default_factory=list)
    final_item_costs: list[int | None] = field(default_factory=list)
    inventory_source: str | None = None
    inventory_game_time_ms: int | None = None
    inventory_layout_exact: bool = False
    net_worth: int | None = None
    net_worth_method: str | None = None
    apm_average: float | None = None
    apm_peak_60s: int | None = None
    apm_peak_game_time_ms: int | None = None
    apm_actions: int = 0

@dataclass
class ItemOrder:
    replay_time_ms: int
    game_time_ms: int
    network_player_id: int
    player_slot: int | None
    player_name: str
    item_rawcode: str
    item_name: str | None

@dataclass(frozen=True)
class InvokerInvokeEvent:
    replay_time_ms: int
    game_time_ms: int
    network_player_id: int
    player_slot: int
    spell_rawcodes: tuple[str, ...]

@dataclass(frozen=True)
class SkillLearnEvent:
    replay_time_ms: int
    game_time_ms: int
    network_player_id: int
    player_slot: int | None
    player_name: str
    hero_rawcode: str | None
    hero_name: str | None
    ability_rawcode: str
    ability_name: str | None
    ability_max_levels: int | None
    new_level: int
    source: str
    confidence: str
    ability_profile_id: str
    is_pregame: bool

@dataclass
class DotaPlayerSnapshot:
    slot: int
    kills: int
    deaths: int
    assists: int
    creep_kills: int
    creep_denies: int
    neutral_kills: int
    gold: int
    item_rawcodes: list[str | None]
    tower_kills: int = 0
    rax_kills: int = 0
    courier_kills: int = 0
    left_time: int = 0

@dataclass
class DotaStatsSnapshot:
    sequence: int
    replay_time_ms: int
    game_time_ms: int
    winner: int | None
    players: list[DotaPlayerSnapshot]
    complete: bool = True

@dataclass
class ItemTiming:
    player_slot: int
    player_name: str
    item_rawcode: str
    item_name: str | None
    earliest_game_time_ms: int
    latest_game_time_ms: int
    precision: str

@dataclass
class KillEvent:
    replay_time_ms: int
    game_time_ms: int
    killer_id: int
    killer_name: str
    killer_hero_rawcode: str | None
    killer_hero_name: str | None
    victim_id: int
    victim_name: str
    victim_hero_rawcode: str | None
    victim_hero_name: str | None

@dataclass
class MultiKillEvent:
    replay_time_ms: int
    game_time_ms: int
    killer_id: int
    killer_name: str
    killer_hero_rawcode: str | None
    killer_hero_name: str | None
    count: int
    label: str
    victim_ids: list[int]
    victim_names: list[str]
    chain_start_game_time_ms: int

@dataclass
class ReplayReport:
    source_file: str
    header: ReplayHeader
    game_name: str = ''
    map_path: str = ''
    map_creator: str = ''
    player_count_declared: int = 0
    game_type: int = 0
    language_id: int = 0
    random_seed: int | None = None
    select_mode: int | None = None
    start_spot_count: int | None = None
    players: list[Player] = field(default_factory=list)
    slots: list[Slot] = field(default_factory=list)
    chats: list[ChatMessage] = field(default_factory=list)
    leaves: list[LeaveEvent] = field(default_factory=list)
    command_packets: list[CommandPacket] = field(default_factory=list)
    string_candidates: list[StringCandidate] = field(default_factory=list)
    gamecache_syncs: list[GameCacheSync] = field(default_factory=list)
    game_start_ms: int | None = None
    dota_players: list[DotaPlayer] = field(default_factory=list)
    dota_stats_snapshots: list[DotaStatsSnapshot] = field(default_factory=list)
    recorded_creep_timelines: RecordedCreepTimelineBundle | None = None
    item_timings: list[ItemTiming] = field(default_factory=list)
    item_orders: list[ItemOrder] = field(default_factory=list)
    skill_learns: list[SkillLearnEvent] = field(default_factory=list)
    invoker_invokes: list[InvokerInvokeEvent] = field(default_factory=list)
    kills: list[KillEvent] = field(default_factory=list)
    multi_kills: list[MultiKillEvent] = field(default_factory=list)
    moments: list[ReplayMoment] = field(default_factory=list)
    block_counts: dict[str, int] = field(default_factory=dict)
    decompressed_bytes: int = 0
    parsed_timeline_ms: int = 0
    warnings: list[str] = field(default_factory=list)
    action_decode_issues: dict[str, int] = field(default_factory=dict)
    command_packet_count: int = 0
    gamecache_sync_count: int = 0
    string_candidate_count: int = 0

    def to_dict(self, include_packets: bool=False) -> dict[str, Any]:
        result = asdict(self)
        if not include_packets:
            result['command_packet_count'] = self.command_packet_count
            result.pop('command_packets', None)
        return result

    def write_json(self, path: str | Path, include_packets: bool=False) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(include_packets=include_packets), ensure_ascii=False, indent=2), encoding='utf-8')
