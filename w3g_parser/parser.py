from __future__ import annotations
import json
import re
import struct
import zlib
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from .actions import decode_actions
from .dota_profile import DOTA_HERO_NAMES
from .item_profile import get_item_definition
REPLAY_SIGNATURE = b'Warcraft III recorded game\x1a\x00'

class ReplayParseError(ValueError):
    pass

class Cursor:

    def __init__(self, data: bytes, offset: int=0) -> None:
        self.data = data
        self.pos = offset

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def peek_u8(self) -> int:
        self.require(1)
        return self.data[self.pos]

    def require(self, size: int) -> None:
        if size < 0 or self.pos + size > len(self.data):
            raise ReplayParseError(f'Unexpected end of data at 0x{self.pos:X}: need {size} bytes, have {self.remaining()}')

    def take(self, size: int) -> bytes:
        self.require(size)
        value = self.data[self.pos:self.pos + size]
        self.pos += size
        return value

    def skip(self, size: int) -> None:
        self.take(size)

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return struct.unpack('<H', self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack('<I', self.take(4))[0]

    def cstring_bytes(self, max_size: int | None=None) -> bytes:
        limit = len(self.data)
        if max_size is not None:
            limit = min(limit, self.pos + max_size)
        end = self.data.find(b'\x00', self.pos, limit)
        if end < 0:
            raise ReplayParseError(f'Unterminated string at 0x{self.pos:X}')
        value = self.data[self.pos:end]
        self.pos = end + 1
        return value

def decode_text(value: bytes) -> str:
    if not value:
        return ''
    for encoding in ('utf-8', 'cp1251', 'cp1252'):
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            pass
    return value.decode('latin-1', errors='replace')

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
    final_gold: int | None = None
    inventory_value: int | None = None
    tower_kills: int | None = None
    rax_kills: int | None = None
    courier_kills: int | None = None
    left_time_seconds: int | None = None
    final_item_rawcodes: list[str | None] = field(default_factory=list)
    final_item_names: list[str | None] = field(default_factory=list)
    final_item_costs: list[int | None] = field(default_factory=list)
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
    item_timings: list[ItemTiming] = field(default_factory=list)
    item_orders: list[ItemOrder] = field(default_factory=list)
    kills: list[KillEvent] = field(default_factory=list)
    multi_kills: list[MultiKillEvent] = field(default_factory=list)
    block_counts: dict[str, int] = field(default_factory=dict)
    decompressed_bytes: int = 0
    parsed_timeline_ms: int = 0
    warnings: list[str] = field(default_factory=list)
    action_decode_issues: dict[str, int] = field(default_factory=dict)
    apm_action_times: dict[int, list[int]] = field(default_factory=dict)
    pending_item_orders: list[tuple[int, int, str]] = field(default_factory=list)

    def to_dict(self, include_packets: bool=False) -> dict[str, Any]:
        result = asdict(self)
        result.pop('apm_action_times', None)
        result.pop('pending_item_orders', None)
        if not include_packets:
            result['command_packet_count'] = len(self.command_packets)
            result.pop('command_packets', None)
        return result

    def write_json(self, path: str | Path, include_packets: bool=False) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(include_packets=include_packets), ensure_ascii=False, indent=2), encoding='utf-8')

def _parse_header(data: bytes) -> ReplayHeader:
    if len(data) < 68:
        raise ReplayParseError('The file is too short to contain a W3G header')
    if data[:len(REPLAY_SIGNATURE)] != REPLAY_SIGNATURE:
        raise ReplayParseError('The file does not have a Warcraft III replay signature')
    cursor = Cursor(data, 28)
    header_size = cursor.u32()
    compressed_size = cursor.u32()
    header_version = cursor.u32()
    decompressed_size = cursor.u32()
    compressed_blocks = cursor.u32()
    if header_version != 1:
        raise ReplayParseError(f'Unsupported W3G header version: {header_version}')
    product_raw = cursor.take(4)
    version = cursor.u32()
    build = cursor.u16()
    flags = cursor.u16()
    duration_ms = cursor.u32()
    checksum = cursor.u32()
    try:
        product = product_raw[::-1].decode('ascii')
    except UnicodeDecodeError:
        product = product_raw.hex()
    if header_size < 68 or header_size > len(data):
        raise ReplayParseError(f'Invalid replay header size: {header_size}')
    return ReplayHeader(header_size=header_size, compressed_size=compressed_size, header_version=header_version, decompressed_size=decompressed_size, compressed_blocks=compressed_blocks, product=product, version=version, build=build, flags=flags, duration_ms=duration_ms, checksum=checksum)

def _decompress_blocks(data: bytes, header: ReplayHeader) -> tuple[bytes, list[str]]:
    cursor = Cursor(data, header.header_size)
    output = bytearray()
    warnings: list[str] = []
    for index in range(header.compressed_blocks):
        if cursor.remaining() < 8:
            raise ReplayParseError(f'Compressed block {index + 1}/{header.compressed_blocks} has no header')
        compressed_size = cursor.u16()
        expected_size = cursor.u16()
        cursor.u32()
        if compressed_size <= 0:
            raise ReplayParseError(f'Compressed block {index + 1} has invalid size {compressed_size}')
        compressed = cursor.take(compressed_size)
        try:
            decompressor = zlib.decompressobj()
            block = decompressor.decompress(compressed) + decompressor.flush()
        except zlib.error:
            try:
                decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
                block = decompressor.decompress(compressed) + decompressor.flush()
            except zlib.error as exc:
                raise ReplayParseError(f'Cannot decompress block {index + 1}: {exc}') from exc
        if len(block) != expected_size:
            warnings.append(f'Block {index + 1}: expected {expected_size} decompressed bytes, got {len(block)}')
        output.extend(block)
    if len(output) < header.decompressed_size:
        warnings.append(f'Header declares {header.decompressed_size} decompressed bytes, blocks contain {len(output)}')
    elif len(output) > header.decompressed_size:
        output = output[:header.decompressed_size]
    return (bytes(output), warnings)

def _parse_player_record(cursor: Cursor) -> Player:
    record_id = cursor.u8()
    if record_id not in (0, 22):
        raise ReplayParseError(f'Expected a player record at 0x{cursor.pos - 1:X}, got 0x{record_id:02X}')
    player_id = cursor.u8()
    name = decode_text(cursor.cstring_bytes(max_size=256))
    additional_size = cursor.u8()
    additional = Cursor(cursor.take(additional_size))
    runtime = additional.u32() if additional.remaining() >= 4 else None
    race_flags = additional.u32() if additional.remaining() >= 4 else None
    return Player(player_id=player_id, name=name, record_id=record_id, runtime=runtime, race_flags=race_flags)

def _decode_game_settings(encoded: bytes) -> bytes:
    decoded = bytearray()
    index = 0
    while index < len(encoded):
        mask = encoded[index]
        index += 1
        for bit in range(1, 8):
            if index >= len(encoded):
                break
            value = encoded[index]
            index += 1
            if mask & 1 << bit:
                decoded.append(value)
            else:
                decoded.append(value - 1 & 255)
    return bytes(decoded)

def _parse_game_settings(encoded: bytes) -> tuple[str, str]:
    decoded = _decode_game_settings(encoded)
    if len(decoded) <= 13:
        return ('', '')
    cursor = Cursor(decoded, 13)
    try:
        map_path = decode_text(cursor.cstring_bytes())
        creator = decode_text(cursor.cstring_bytes())
        return (map_path, creator)
    except ReplayParseError:
        return (decode_text(decoded[13:]), '')

def _parse_initial_data(data: bytes, report: ReplayReport) -> int:
    cursor = Cursor(data)
    cursor.skip(4)
    report.players.append(_parse_player_record(cursor))
    report.game_name = decode_text(cursor.cstring_bytes(max_size=1024))
    cursor.skip(1)
    encoded_settings = cursor.cstring_bytes(max_size=8192)
    report.map_path, report.map_creator = _parse_game_settings(encoded_settings)
    report.player_count_declared = cursor.u32()
    report.game_type = cursor.u32()
    report.language_id = cursor.u32()
    while cursor.remaining() and cursor.peek_u8() == 22:
        report.players.append(_parse_player_record(cursor))
        cursor.skip(4)
    if not cursor.remaining() or cursor.peek_u8() != 25:
        raise ReplayParseError(f'Expected game-start record at 0x{cursor.pos:X}, got 0x{cursor.peek_u8():02X}')
    start = cursor.pos
    cursor.u8()
    following_size = cursor.u16()
    end = start + 3 + following_size
    if end > len(data):
        raise ReplayParseError('Truncated game-start record')
    slot_count = cursor.u8()
    for _ in range(slot_count):
        report.slots.append(Slot(player_id=cursor.u8(), download_status=cursor.u8(), slot_status=cursor.u8(), computer=bool(cursor.u8()), team=cursor.u8(), color=cursor.u8(), race=cursor.u8(), computer_type=cursor.u8(), handicap=cursor.u8()))
    report.random_seed = cursor.u32()
    report.select_mode = cursor.u8()
    report.start_spot_count = cursor.u8()
    cursor.pos = end
    return cursor.pos
PRINTABLE_RUN_RE = re.compile(b'[\\x20-\\x7e\\x80-\\xff]{4,}')

def _plausible_candidate(text: str) -> bool:
    cleaned = text.strip()
    if len(cleaned) < 4 or len(cleaned) > 300:
        return False
    if 'ÿ' in cleaned or '�' in cleaned:
        return False
    printable = sum((character.isprintable() for character in cleaned))
    letters = sum((character.isalpha() for character in cleaned))
    if printable / len(cleaned) <= 0.9 or letters < 2:
        return False
    if re.fullmatch('[A-Za-z0-9]{4,8}', cleaned):
        return False
    return len(cleaned) >= 12 or any((character.isspace() for character in cleaned)) or cleaned.startswith(('-', '!', '/'))
GAMECACHE_FIELD_RE = re.compile('^[A-Za-z0-9_.:-]{0,80}$')

def _extract_gamecache_syncs(payload: bytes, time_ms: int, player_id: int) -> list[GameCacheSync]:
    events: list[GameCacheSync] = []
    index = 0
    while index < len(payload):
        if payload[index] != 107:
            index += 1
            continue
        cursor = Cursor(payload, index + 1)
        try:
            cache_name = decode_text(cursor.cstring_bytes(max_size=82))
            mission_key = decode_text(cursor.cstring_bytes(max_size=82))
            key = decode_text(cursor.cstring_bytes(max_size=8192))
            raw_value = cursor.take(4)
        except ReplayParseError:
            index += 1
            continue
        ordinary_fields = all((GAMECACHE_FIELD_RE.fullmatch(field) is not None for field in (cache_name, mission_key, key)))
        dota_json_chunk = cache_name == 'dr.x' and mission_key == 'game_stats' and (0 < len(key) <= 8192)
        if not ordinary_fields and (not dota_json_chunk):
            index += 1
            continue
        if not cache_name or not key:
            index += 1
            continue
        value_u32 = struct.unpack('<I', raw_value)[0]
        value_i32 = struct.unpack('<i', raw_value)[0]
        value_ascii = raw_value.decode('ascii') if all((32 <= value <= 126 for value in raw_value)) else None
        events.append(GameCacheSync(time_ms=time_ms, player_id=player_id, cache_name=cache_name, mission_key=mission_key, key=key, value_u32=value_u32, value_i32=value_i32, value_hex=raw_value.hex().upper(), value_ascii=value_ascii, packet_offset=index))
        index = cursor.pos
    return events

def _extract_strings(payload: bytes, time_ms: int, player_id: int) -> list[StringCandidate]:
    candidates: list[StringCandidate] = []
    seen: set[tuple[int, str]] = set()
    for match in PRINTABLE_RUN_RE.finditer(payload):
        text = decode_text(match.group()).strip()
        key = (match.start(), text)
        if key not in seen and _plausible_candidate(text):
            seen.add(key)
            candidates.append(StringCandidate(time_ms=time_ms, player_id=player_id, source='printable-run', text=text, packet_offset=match.start()))
    for index, value in enumerate(payload):
        if value != 96 or index + 10 > len(payload):
            continue
        first, second = struct.unpack_from('<II', payload, index + 1)
        if first != second:
            continue
        end = payload.find(b'\x00', index + 9, min(len(payload), index + 310))
        if end < 0:
            continue
        text = decode_text(payload[index + 9:end]).strip()
        key = (index + 9, text)
        if key not in seen and _plausible_candidate(text):
            seen.add(key)
            candidates.append(StringCandidate(time_ms=time_ms, player_id=player_id, source='map-trigger-chat', text=text, packet_offset=index))
    return candidates

def _parse_command_data(payload: bytes, time_ms: int, report: ReplayReport) -> None:
    cursor = Cursor(payload)
    while cursor.remaining() >= 3:
        player_id = cursor.u8()
        action_size = cursor.u16()
        if action_size > cursor.remaining():
            report.warnings.append(f'Truncated command packet at {time_ms} ms: declared {action_size}, have {cursor.remaining()}')
            return
        actions = cursor.take(action_size)
        report.command_packets.append(CommandPacket(time_ms=time_ms, player_id=player_id, byte_length=action_size))
        decoded_actions, issue = decode_actions(actions)
        for action in decoded_actions:
            counts_for_apm = action.counts_for_apm
            previous = getattr(report, '_last_decoded_action', None)
            if action.action_id == 22 and action.selection_mode == 1 and (previous is not None) and (previous[0] == player_id) and (previous[1] == 22) and (previous[2] == 2):
                counts_for_apm = False
            if counts_for_apm:
                report.apm_action_times.setdefault(player_id, []).append(time_ms)
            if action.ability_rawcode and action.ability_rawcode.startswith('I'):
                report.pending_item_orders.append((time_ms, player_id, action.ability_rawcode))
            report._last_decoded_action = (player_id, action.action_id, action.selection_mode)
        if issue is not None:
            issue_key = f'0x{issue.action_id:02X}:{issue.reason}'
            report.action_decode_issues[issue_key] = report.action_decode_issues.get(issue_key, 0) + 1
        report.gamecache_syncs.extend(_extract_gamecache_syncs(actions, time_ms=time_ms, player_id=player_id))
        report.string_candidates.extend(_extract_strings(actions, time_ms=time_ms, player_id=player_id))
    if cursor.remaining():
        report.warnings.append(f'{cursor.remaining()} trailing command byte(s) at {time_ms} ms')

def _parse_timeline(data: bytes, offset: int, report: ReplayReport) -> None:
    cursor = Cursor(data, offset)
    current_time = 0
    counts: Counter[int] = Counter()
    while cursor.remaining():
        record_offset = cursor.pos
        record_id = cursor.u8()
        counts[record_id] += 1
        try:
            if record_id == 23:
                reason = cursor.u32()
                player_id = cursor.u8()
                result = cursor.u32()
                unknown = cursor.u32()
                report.leaves.append(LeaveEvent(time_ms=current_time, player_id=player_id, reason=reason, result=result, unknown=unknown))
            elif record_id in (26, 27, 28):
                cursor.skip(4)
            elif record_id in (30, 31):
                payload_size = cursor.u16()
                payload = Cursor(cursor.take(payload_size))
                increment = payload.u16()
                current_time += increment
                _parse_command_data(payload.take(payload.remaining()), current_time, report)
            elif record_id == 32:
                player_id = cursor.u8()
                following_size = cursor.u16()
                body = Cursor(cursor.take(following_size))
                flags = body.u8()
                mode = body.u32() if flags == 32 and body.remaining() >= 4 else None
                text = decode_text(body.cstring_bytes()) if body.remaining() else ''
                report.chats.append(ChatMessage(time_ms=current_time, player_id=player_id, mode=mode, text=text))
            elif record_id == 34:
                cursor.skip(5)
            elif record_id == 35:
                cursor.skip(10)
            elif record_id == 47:
                cursor.skip(8)
            elif record_id == 0 and all((value == 0 for value in data[record_offset:])):
                break
            else:
                report.warnings.append(f'Unknown replay record 0x{record_id:02X} at decompressed offset 0x{record_offset:X}; timeline stopped')
                break
        except ReplayParseError as exc:
            report.warnings.append(f'Cannot parse record 0x{record_id:02X} at decompressed offset 0x{record_offset:X}: {exc}')
            break
    report.parsed_timeline_ms = current_time
    report.block_counts = {f'0x{record_id:02X}': count for record_id, count in sorted(counts.items())}
MULTI_KILL_WINDOW_MS = 18000
MULTI_KILL_LABELS = {2: 'Double Kill', 3: 'Triple Kill', 4: 'Ultra Kill'}
FINAL_STAT_KEYS = {'0': 'level', '1': 'kills', '2': 'deaths', '3': 'creep_kills', '4': 'creep_denies', '5': 'assists', '6': 'final_gold', '7': 'neutral_kills'}

def _latest_sync(report: ReplayReport, mission_key: str, key: str) -> GameCacheSync | None:
    matches = [event for event in report.gamecache_syncs if event.cache_name == 'dr.x' and event.mission_key == mission_key and (event.key == key)]
    return max(matches, key=lambda event: event.time_ms) if matches else None

def _rawcode_from_sync(event: GameCacheSync | None) -> str | None:
    if event is None or event.value_ascii is None or len(event.value_ascii) != 4:
        return None
    return event.value_ascii[::-1]

def _peak_apm_60s(times: list[int]) -> tuple[int, int | None]:
    if not times:
        return (0, None)
    left = 0
    best_count = 0
    best_end: int | None = None
    for right, current in enumerate(times):
        while current - times[left] >= 60000:
            left += 1
        count = right - left + 1
        if count > best_count:
            best_count = count
            best_end = current
    return (best_count, best_end)

def _rawcode_from_integer(value: int) -> str | None:
    if value == 0:
        return None
    try:
        rawcode = value.to_bytes(4, 'big').decode('ascii')
    except (OverflowError, UnicodeDecodeError):
        return None
    return rawcode if re.fullmatch('[A-Za-z0-9]{4}', rawcode) else None

def _extract_dota_stats_snapshots(report: ReplayReport, game_start_ms: int) -> list[DotaStatsSnapshot]:
    buffers: dict[int, str] = {}
    candidates: dict[int, list[DotaStatsSnapshot]] = defaultdict(list)
    marker_re = re.compile('end\\s+(\\d+)$')
    for event in report.gamecache_syncs:
        if event.cache_name != 'dr.x' or event.mission_key != 'game_stats':
            continue
        marker = marker_re.fullmatch(event.key)
        if event.key.startswith('{'):
            buffers[event.player_id] = event.key
            continue
        if marker is None:
            if event.player_id in buffers:
                buffers[event.player_id] += event.key
            continue
        raw_json = buffers.pop(event.player_id, '')
        if not raw_json:
            continue
        try:
            payload = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError):
            continue
        raw_players = payload.get('players')
        if not isinstance(raw_players, list):
            continue
        players: list[DotaPlayerSnapshot] = []
        for raw_player in raw_players:
            if not isinstance(raw_player, dict):
                continue
            raw_items = raw_player.get('items', [])
            items = [_rawcode_from_integer(value) for value in raw_items if isinstance(value, int)][:6]
            items.extend([None] * (6 - len(items)))
            players.append(DotaPlayerSnapshot(slot=int(raw_player.get('id', 0)), kills=int(raw_player.get('kills', 0)), deaths=int(raw_player.get('deaths', 0)), assists=int(raw_player.get('assists', 0)), creep_kills=int(raw_player.get('creep_kills', 0)), creep_denies=int(raw_player.get('creep_denies', 0)), neutral_kills=int(raw_player.get('neutral_kills', 0)), gold=int(raw_player.get('gold', 0)), item_rawcodes=items, tower_kills=int(raw_player.get('tower_kills', 0)), rax_kills=int(raw_player.get('rax_kills', 0)), courier_kills=int(raw_player.get('courier_kills', 0)), left_time=int(raw_player.get('left_time', 0))))
        sequence = int(marker.group(1))
        winner = payload.get('winner')
        candidates[sequence].append(DotaStatsSnapshot(sequence=sequence, replay_time_ms=event.time_ms, game_time_ms=max(event.time_ms - game_start_ms, 0), winner=int(winner) if isinstance(winner, int) else None, players=players))
    snapshots = [min(sequence_candidates, key=lambda snapshot: snapshot.replay_time_ms) for _, sequence_candidates in sorted(candidates.items())]
    return snapshots

def _derive_item_timings(report: ReplayReport, player_names: dict[int, str]) -> None:
    previous_items: dict[int, Counter[str]] = defaultdict(Counter)
    previous_time: dict[int, int] = defaultdict(int)
    for snapshot in report.dota_stats_snapshots:
        for player in snapshot.players:
            current = Counter((rawcode for rawcode in player.item_rawcodes if rawcode is not None))
            additions = current - previous_items[player.slot]
            for rawcode, count in additions.items():
                for _ in range(count):
                    report.item_timings.append(ItemTiming(player_slot=player.slot, player_name=player_names.get(player.slot, f'Player {player.slot}'), item_rawcode=rawcode, item_name=get_item_definition(rawcode).name if get_item_definition(rawcode) is not None else None, earliest_game_time_ms=previous_time[player.slot], latest_game_time_ms=snapshot.game_time_ms, precision='snapshot-window'))
            previous_items[player.slot] = current
            previous_time[player.slot] = snapshot.game_time_ms

def _derive_dota_events(report: ReplayReport) -> None:
    network_player_names = {player.player_id: player.name or f'Player {player.player_id}' for player in report.players}
    player_names = {slot.color: network_player_names.get(slot.player_id, f'Player {slot.player_id}') for slot in report.slots if slot.slot_status == 2 and (not slot.computer)}
    hero_by_slot: dict[int, tuple[str | None, str | None]] = {}
    for slot in report.slots:
        if slot.slot_status != 2 or slot.computer:
            continue
        rawcode = _rawcode_from_sync(_latest_sync(report, str(slot.color), '9'))
        hero_name = DOTA_HERO_NAMES.get(rawcode) if rawcode else None
        hero_by_slot[slot.color] = (rawcode, hero_name)
        report.dota_players.append(DotaPlayer(slot=slot.color, network_player_id=slot.player_id, name=player_names.get(slot.color, f'Player {slot.color}'), hero_rawcode=rawcode, hero_name=hero_name))
    report.dota_players.sort(key=lambda player: player.slot)
    start_events = [event.time_ms for event in report.gamecache_syncs if event.cache_name == 'dr.x' and event.mission_key == 'Data' and (event.key == 'GameStart') and (event.value_i32 == 1)]
    report.game_start_ms = min(start_events) if start_events else None
    game_start = report.game_start_ms or 0
    report.dota_stats_snapshots = _extract_dota_stats_snapshots(report, game_start)
    _derive_item_timings(report, player_names)
    dota_by_slot = {player.slot: player for player in report.dota_players}
    slot_by_network_id = {player.network_player_id: player.slot for player in report.dota_players}
    for dota_player in report.dota_players:
        mission_key = str(dota_player.slot)
        for key, attribute in FINAL_STAT_KEYS.items():
            event = _latest_sync(report, mission_key, key)
            if event is not None:
                setattr(dota_player, attribute, event.value_i32)
        dota_player.final_item_rawcodes = [_rawcode_from_sync(_latest_sync(report, mission_key, f'8_{index}')) for index in range(6)]
        item_definitions = [get_item_definition(rawcode) for rawcode in dota_player.final_item_rawcodes]
        dota_player.final_item_names = [definition.name if definition is not None else None for definition in item_definitions]
        dota_player.final_item_costs = [definition.cost if definition is not None else None for definition in item_definitions]
        if all((rawcode is None or definition is not None for rawcode, definition in zip(dota_player.final_item_rawcodes, item_definitions))):
            dota_player.inventory_value = sum((definition.cost for definition in item_definitions if definition is not None))
            if dota_player.final_gold is not None:
                dota_player.net_worth = dota_player.final_gold + dota_player.inventory_value
                dota_player.net_worth_method = 'final_gold_plus_six_slot_inventory'
        dota_player.side = 'Sentinel' if dota_player.slot <= 5 else 'Scourge'
        leave_times = [event.time_ms for event in report.leaves if event.player_id == dota_player.network_player_id and event.time_ms > game_start]
        end_time = min(leave_times) if leave_times else report.parsed_timeline_ms
        action_times = sorted((time_ms for time_ms in report.apm_action_times.get(dota_player.network_player_id, []) if game_start <= time_ms <= end_time))
        dota_player.apm_actions = len(action_times)
        active_minutes = max(end_time - game_start, 1) / 60000
        dota_player.apm_average = len(action_times) / active_minutes
        peak, peak_end = _peak_apm_60s(action_times)
        dota_player.apm_peak_60s = peak
        dota_player.apm_peak_game_time_ms = peak_end - game_start if peak_end is not None else None
    if report.dota_stats_snapshots:
        final_snapshot = report.dota_stats_snapshots[-1]
        final_by_slot = {player.slot: player for player in final_snapshot.players}
        for dota_player in report.dota_players:
            snapshot_player = final_by_slot.get(dota_player.slot)
            if snapshot_player is not None:
                dota_player.tower_kills = snapshot_player.tower_kills
                dota_player.rax_kills = snapshot_player.rax_kills
                dota_player.courier_kills = snapshot_player.courier_kills
                dota_player.left_time_seconds = snapshot_player.left_time
            if final_snapshot.winner in (1, 2):
                winning_side = 'Sentinel' if final_snapshot.winner == 1 else 'Scourge'
                dota_player.won = dota_player.side == winning_side
    for time_ms, network_player_id, item_rawcode in report.pending_item_orders:
        slot = slot_by_network_id.get(network_player_id)
        dota_player = dota_by_slot.get(slot) if slot is not None else None
        report.item_orders.append(ItemOrder(replay_time_ms=time_ms, game_time_ms=time_ms - game_start, network_player_id=network_player_id, player_slot=slot, player_name=dota_player.name if dota_player is not None else network_player_names.get(network_player_id, f'Player {network_player_id}'), item_rawcode=item_rawcode, item_name=get_item_definition(item_rawcode).name if get_item_definition(item_rawcode) is not None else None))
    seen_kills: set[tuple[int, int, int]] = set()
    for event in report.gamecache_syncs:
        if event.cache_name != 'dr.x' or event.mission_key != 'Data':
            continue
        match = re.fullmatch('Hero(\\d+)', event.key)
        if match is None:
            continue
        victim_id = int(match.group(1))
        killer_id = event.value_i32
        identity = (event.time_ms, killer_id, victim_id)
        if identity in seen_kills:
            continue
        seen_kills.add(identity)
        report.kills.append(KillEvent(replay_time_ms=event.time_ms, game_time_ms=event.time_ms - game_start, killer_id=killer_id, killer_name=player_names.get(killer_id, f'Player {killer_id}'), killer_hero_rawcode=hero_by_slot.get(killer_id, (None, None))[0], killer_hero_name=hero_by_slot.get(killer_id, (None, None))[1], victim_id=victim_id, victim_name=player_names.get(victim_id, f'Player {victim_id}'), victim_hero_rawcode=hero_by_slot.get(victim_id, (None, None))[0], victim_hero_name=hero_by_slot.get(victim_id, (None, None))[1]))
    report.kills.sort(key=lambda event: event.replay_time_ms)
    chains: dict[int, list[KillEvent]] = {}
    for kill in report.kills:
        if kill.killer_id not in player_names or kill.killer_id == kill.victim_id:
            continue
        chain = chains.get(kill.killer_id, [])
        if not chain or kill.replay_time_ms - chain[-1].replay_time_ms > MULTI_KILL_WINDOW_MS:
            chain = [kill]
        else:
            chain.append(kill)
        chains[kill.killer_id] = chain
        count = len(chain)
        if count < 2:
            continue
        label = MULTI_KILL_LABELS.get(count, 'Rampage')
        report.multi_kills.append(MultiKillEvent(replay_time_ms=kill.replay_time_ms, game_time_ms=kill.game_time_ms, killer_id=kill.killer_id, killer_name=kill.killer_name, killer_hero_rawcode=kill.killer_hero_rawcode, killer_hero_name=kill.killer_hero_name, count=count, label=label, victim_ids=[event.victim_id for event in chain], victim_names=[event.victim_name for event in chain], chain_start_game_time_ms=chain[0].game_time_ms))

def parse_replay(path: str | Path) -> ReplayReport:
    source = Path(path)
    data = source.read_bytes()
    header = _parse_header(data)
    decompressed, warnings = _decompress_blocks(data, header)
    report = ReplayReport(source_file=str(source.resolve()), header=header, decompressed_bytes=len(decompressed), warnings=warnings)
    timeline_offset = _parse_initial_data(decompressed, report)
    _parse_timeline(decompressed, timeline_offset, report)
    _derive_dota_events(report)
    return report
