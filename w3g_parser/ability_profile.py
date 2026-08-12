from __future__ import annotations
import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from .native_runtime_host import NativeRuntimeError, native_file_sha256
PROFILE_DIRECTORY = Path(__file__).with_name('profiles')
PROFILE_PATH = PROFILE_DIRECTORY / 'iccup_dota_500_abilities.json'
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

def _load_ability_profile(path: Path) -> AbilityProfile | None:
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
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

@lru_cache(maxsize=1)
def load_ability_profiles() -> tuple[AbilityProfile, ...]:
    profiles: list[AbilityProfile] = []
    paths = sorted(PROFILE_DIRECTORY.glob('*_abilities.json'))
    local_app_data = os.environ.get('LOCALAPPDATA')
    if local_app_data:
        paths.extend(sorted((Path(local_app_data) / 'ReplayLab' / 'profiles').glob('*.json')))
    seen_ids: set[str] = set()
    for path in paths:
        profile = _load_ability_profile(path)
        if profile is not None and profile.profile_id not in seen_ids:
            seen_ids.add(profile.profile_id)
            profiles.append(profile)
    return tuple(profiles)

@lru_cache(maxsize=1)
def load_ability_profile() -> AbilityProfile | None:
    profiles = load_ability_profiles()
    return profiles[0] if profiles else None

def get_ability_profile(map_path: str) -> AbilityProfile | None:
    filename = map_path.replace('/', '\\').rsplit('\\', 1)[-1]
    profiles = load_ability_profiles()
    for profile in profiles:
        if filename.casefold() == profile.map_name.casefold():
            return profile
    local_path = Path(map_path)
    if local_path.is_file():
        try:
            digest = native_file_sha256(local_path).lower()
        except NativeRuntimeError:
            return None
        for profile in profiles:
            if digest == profile.map_sha256:
                return profile
    return None

@lru_cache(maxsize=1)
def get_ability_catalog() -> dict[str, AbilityDefinition]:
    result: dict[str, AbilityDefinition] = {}
    for profile in load_ability_profiles():
        for rawcode, definition in profile.abilities.items():
            result.setdefault(rawcode, definition)
    return result

def supports_skill_timeline(map_path: str) -> bool:
    return get_ability_profile(map_path) is not None

def get_ability_definition(rawcode: str | None) -> AbilityDefinition | None:
    if rawcode is None:
        return None
    return get_ability_catalog().get(rawcode)
