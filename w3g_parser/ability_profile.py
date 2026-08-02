from __future__ import annotations
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
PROFILE_PATH = Path(__file__).with_name('profiles') / 'iccup_dota_500_abilities.json'
SHA256_RE = re.compile('^[0-9a-f]{64}$')

@dataclass(frozen=True)
class AbilityDefinition:
    rawcode: str
    name: str
    max_levels: int
    art_path: str | None = None
    button_x: int | None = None
    button_y: int | None = None

@dataclass(frozen=True)
class AbilityProfile:
    profile_id: str
    schema_version: int
    map_name: str
    map_sha256: str
    source: str
    abilities: dict[str, AbilityDefinition]

@lru_cache(maxsize=1)
def load_ability_profile() -> AbilityProfile | None:
    try:
        payload = json.loads(PROFILE_PATH.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    profile_id = payload.get('profile_id')
    map_name = payload.get('map')
    map_sha256 = payload.get('map_sha256')
    source = payload.get('source')
    if payload.get('schema_version') != 1 or not isinstance(profile_id, str) or (not profile_id) or (not isinstance(map_name, str)) or (not map_name) or (not isinstance(map_sha256, str)) or (SHA256_RE.fullmatch(map_sha256) is None) or (not isinstance(source, str)) or (not source):
        return None
    values = payload.get('abilities', {})
    if not isinstance(values, dict):
        return None
    result: dict[str, AbilityDefinition] = {}
    for rawcode, entry in values.items():
        if not isinstance(rawcode, str) or len(rawcode) != 4 or (not isinstance(entry, dict)) or (not isinstance(entry.get('name'), str)):
            continue
        try:
            max_levels = int(entry.get('max_levels', 0))
        except (TypeError, ValueError):
            continue
        if max_levels <= 0:
            continue
        result[rawcode] = AbilityDefinition(rawcode=rawcode, name=entry['name'].strip(), max_levels=max_levels, art_path=entry.get('art_path') if isinstance(entry.get('art_path'), str) else None, button_x=entry.get('button_x') if isinstance(entry.get('button_x'), int) else None, button_y=entry.get('button_y') if isinstance(entry.get('button_y'), int) else None)
    if not result:
        return None
    return AbilityProfile(profile_id=profile_id, schema_version=1, map_name=map_name, map_sha256=map_sha256, source=source, abilities=result)

def get_ability_profile(map_path: str) -> AbilityProfile | None:
    profile = load_ability_profile()
    if profile is None:
        return None
    filename = map_path.replace('/', '\\').rsplit('\\', 1)[-1]
    if filename.casefold() != profile.map_name.casefold():
        return None
    return profile

def supports_skill_timeline(map_path: str) -> bool:
    return get_ability_profile(map_path) is not None

def get_ability_definition(rawcode: str | None) -> AbilityDefinition | None:
    profile = load_ability_profile()
    if rawcode is None or profile is None:
        return None
    return profile.abilities.get(rawcode)
