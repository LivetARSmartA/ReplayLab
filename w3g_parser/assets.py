from __future__ import annotations
import json
import re
import sys
from functools import lru_cache
from pathlib import Path
HERO_ALIASES = {'Stealth Assassin': 'Stealth Assasin', 'Lightning Revenant': 'Lighting Revenant'}
ITEM_ALIASES = {'Blink Dagger': 'Kelens Dagger of Escape', "Kelen's Dagger": 'Kelens Dagger of Escape', 'Boots of Travel Level 1': 'Boots of Travel', 'Boots of Travel Level 2': 'Boots of Travel 2', 'Magical Bottle - 3/3': 'Empty Bottle', 'Magical Bottle - 2/3': 'Empty Bottle', 'Magical Bottle - 1/3': 'Empty Bottle', 'Observer and Sentry Wards': 'Observer Wards', 'Sentry and Observer Wards': 'Sentry Wards', "Vladmir's Offering": 'Vladimirs Offering', 'Gauntlets of Strength': 'Gauntlets of Ogre Strength', 'Cranium Basher Recipe': 'Recipe Scroll', "Shiva's Guard Recipe": 'Recipe Scroll', 'Soul Ring Recipe': 'Recipe Scroll'}

def project_root() -> Path:
    frozen_root = getattr(sys, '_MEIPASS', None)
    if frozen_root:
        return Path(frozen_root)
    return Path(__file__).resolve().parents[1]

def app_icon_path() -> Path | None:
    for filename in ('replay_lab_icon_512.png', 'replay_lab.ico'):
        path = project_root() / 'assets' / 'app' / filename
        if path.is_file():
            return path
    return None

@lru_cache(maxsize=1)
def release_build_id() -> str | None:
    manifest_path = project_root() / 'release_manifest.json'
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    build_id = manifest.get('build_id')
    if not isinstance(build_id, str) or not build_id.strip():
        return None
    return build_id.strip()

@lru_cache(maxsize=1)
def load_asset_manifest() -> dict[str, object]:
    manifest_path = project_root() / 'assets' / 'iccup' / 'manifest.json'
    if not manifest_path.is_file():
        return {}
    try:
        return json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}

def _clean_asset_name(name: str) -> str:
    name = re.sub('\\|c[0-9a-fA-F]{8}', '', name)
    name = name.replace('|r', '')
    return ' '.join(name.split())

def _name_key(name: str) -> str:
    return ''.join((character for character in name.casefold() if character.isalnum()))

@lru_cache(maxsize=2)
def _asset_name_index(kind: str) -> dict[str, str]:
    entries = load_asset_manifest().get(kind, {})
    if not isinstance(entries, dict):
        return {}
    result: dict[str, str] = {}
    for name in entries:
        if isinstance(name, str):
            result.setdefault(_name_key(name), name)
    return result

def _asset_name_candidates(kind: str, name: str) -> list[str]:
    cleaned = _clean_asset_name(name)
    aliases = HERO_ALIASES if kind == 'heroes' else ITEM_ALIASES
    candidates = [aliases.get(cleaned, cleaned)]
    without_parentheses = re.sub('\\s*\\([^)]*\\)\\s*', ' ', cleaned).strip()
    candidates.append(aliases.get(without_parentheses, without_parentheses))
    candidates.append(re.sub('\\s+Level\\s+\\d+$', '', cleaned, flags=re.I))
    candidates.append(re.sub('\\s*-\\s*\\d+/\\d+$', '', cleaned))
    if cleaned.casefold().startswith('the '):
        candidates.append(cleaned[4:])
    return list(dict.fromkeys((candidate for candidate in candidates if candidate)))

def _asset_path(kind: str, name: str | None) -> Path | None:
    if not name:
        return None
    entries = load_asset_manifest().get(kind, {})
    if not isinstance(entries, dict):
        return None
    index = _asset_name_index(kind)
    for candidate in _asset_name_candidates(kind, name):
        catalog_name = candidate
        if catalog_name not in entries:
            catalog_name = index.get(_name_key(candidate), '')
        entry = entries.get(catalog_name)
        if not isinstance(entry, dict):
            continue
        relative = entry.get('path')
        if not isinstance(relative, str):
            continue
        path = project_root() / 'assets' / Path(relative)
        if path.is_file():
            return path
    return None

def hero_icon_path(hero_name: str | None) -> Path | None:
    return _asset_path('heroes', hero_name)

def item_icon_path(item_name: str | None) -> Path | None:
    return _asset_path('items', item_name)

@lru_cache(maxsize=1)
def load_ability_icon_manifest() -> dict[str, str]:
    manifest_path = project_root() / 'assets' / 'warcraft' / 'abilities' / 'manifest.json'
    if not manifest_path.is_file():
        return {}
    try:
        payload = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = payload.get('abilities', {})
    if not isinstance(entries, dict):
        return {}
    return {rawcode: relative for rawcode, relative in entries.items() if isinstance(rawcode, str) and isinstance(relative, str)}

def ability_icon_path(rawcode: str | None) -> Path | None:
    if not rawcode:
        return None
    relative = load_ability_icon_manifest().get(rawcode)
    if relative is None:
        return None
    path = project_root() / 'assets' / 'warcraft' / 'abilities' / relative
    return path if path.is_file() else None

def command_icon_path(command: str) -> Path | None:
    manifest_path = project_root() / 'assets' / 'warcraft' / 'abilities' / 'manifest.json'
    try:
        payload = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    entries = payload.get('commands', {})
    if not isinstance(entries, dict):
        return None
    relative = entries.get(command)
    if not isinstance(relative, str):
        return None
    path = manifest_path.parent / relative
    return path if path.is_file() else None
