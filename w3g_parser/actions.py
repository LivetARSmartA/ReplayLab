from __future__ import annotations
import re
import struct
from dataclasses import dataclass

@dataclass(frozen=True)
class DecodedAction:
    action_id: int
    offset: int
    size: int
    counts_for_apm: bool
    selection_mode: int | None = None
    ability_rawcode: str | None = None
    ability_order_id: int | None = None
    ability_flags: int | None = None
    ability_object_id_1: int | None = None
    ability_object_id_2: int | None = None

@dataclass(frozen=True)
class ActionDecodeIssue:
    offset: int
    action_id: int
    reason: str
FIXED_SIZES_126: dict[int, int] = {1: 1, 2: 1, 3: 2, 4: 1, 5: 1, 7: 5, 24: 3, 25: 13, 26: 1, 27: 10, 28: 10, 29: 9, 30: 6, 32: 1, 33: 9, 34: 1, 35: 1, 36: 1, 37: 1, 38: 1, 39: 6, 40: 6, 41: 1, 42: 1, 43: 1, 44: 1, 45: 6, 46: 5, 47: 1, 48: 1, 49: 1, 50: 1, 80: 6, 81: 10, 97: 1, 98: 13, 102: 1, 103: 1, 104: 13, 105: 17, 106: 17, 117: 2}
APM_ACTION_IDS = {16, 17, 18, 19, 20, 22, 23, 24, 28, 29, 30, 97, 102, 103}
RAWCODE_RE = re.compile('^[A-Za-z0-9]{4}$')

def _read_cstring_end(payload: bytes, start: int, max_size: int | None=512) -> int:
    limit = len(payload)
    if max_size is not None:
        limit = min(limit, start + max_size)
    end = payload.find(b'\x00', start, limit)
    if end < 0:
        raise ValueError('unterminated string')
    return end + 1

def _ability_size(action_id: int) -> int:
    base = 15
    if action_id == 16:
        return base
    if action_id == 17:
        return base + 8
    if action_id == 18:
        return base + 16
    if action_id == 19:
        return base + 24
    if action_id == 20:
        return base + 8 + 4 + 9 + 8
    raise ValueError(f'unsupported ability action 0x{action_id:02X}')

def _decode_ability_rawcode(payload: bytes, offset: int) -> str | None:
    raw = payload[offset + 3:offset + 7]
    if len(raw) != 4:
        return None
    try:
        code = raw[::-1].decode('ascii')
    except UnicodeDecodeError:
        return None
    return code if RAWCODE_RE.fullmatch(code) else None

def _gamecache_size(payload: bytes, offset: int) -> int:
    cursor = offset + 1
    for _ in range(3):
        cursor = _read_cstring_end(payload, cursor, max_size=None)
    if cursor + 4 > len(payload):
        raise ValueError('truncated gamecache value')
    return cursor + 4 - offset

def decode_actions(payload: bytes) -> tuple[list[DecodedAction], ActionDecodeIssue | None]:
    actions: list[DecodedAction] = []
    offset = 0
    while offset < len(payload):
        action_id = payload[offset]
        selection_mode: int | None = None
        ability_rawcode: str | None = None
        ability_order_id: int | None = None
        ability_flags: int | None = None
        ability_object_id_1: int | None = None
        ability_object_id_2: int | None = None
        try:
            if action_id == 6:
                size = _read_cstring_end(payload, offset + 1) - offset
            elif action_id in (16, 17, 18, 19, 20):
                size = _ability_size(action_id)
                ability_rawcode = _decode_ability_rawcode(payload, offset)
                ability_order_id = struct.unpack_from('<I', payload, offset + 3)[0]
                ability_flags = struct.unpack_from('<H', payload, offset + 1)[0]
                ability_object_id_1, ability_object_id_2 = struct.unpack_from('<II', payload, offset + 7)
            elif action_id in (22, 23):
                if offset + 4 > len(payload):
                    raise ValueError('truncated selection header')
                count = struct.unpack_from('<H', payload, offset + 2)[0]
                size = 4 + 8 * count
                if action_id == 22:
                    selection_mode = payload[offset + 1]
            elif action_id == 96:
                size = _read_cstring_end(payload, offset + 9) - offset
            elif action_id == 107:
                size = _gamecache_size(payload, offset)
            else:
                size = FIXED_SIZES_126[action_id]
        except (KeyError, ValueError, struct.error) as exc:
            return (actions, ActionDecodeIssue(offset=offset, action_id=action_id, reason=str(exc) or 'unknown action'))
        if size <= 0 or offset + size > len(payload):
            return (actions, ActionDecodeIssue(offset=offset, action_id=action_id, reason=f'action size {size} exceeds packet length'))
        actions.append(DecodedAction(action_id=action_id, offset=offset, size=size, counts_for_apm=action_id in APM_ACTION_IDS, selection_mode=selection_mode, ability_rawcode=ability_rawcode, ability_order_id=ability_order_id, ability_flags=ability_flags, ability_object_id_1=ability_object_id_1, ability_object_id_2=ability_object_id_2))
        offset += size
    return (actions, None)
