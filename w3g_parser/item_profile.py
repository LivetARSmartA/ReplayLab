from __future__ import annotations
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

@dataclass(frozen=True)
class ItemDefinition:
    rawcode: str
    cost: int
    name: str | None
    name_confidence: float | None = None
    name_source: str | None = None

def canonical_item_name(name: str | None) -> str | None:
    if not name:
        return None
    name = re.sub('\\|c[0-9a-fA-F]{8}', '', name).replace('|r', '')
    name = ' '.join(name.split())
    name = re.sub('\\s*\\([^)]*\\)\\s*$', '', name).strip()
    name = re.sub('\\s+Level\\s+\\d+$', '', name, flags=re.I).strip()
    return name or None

@lru_cache(maxsize=1)
def load_item_profile() -> dict[str, ItemDefinition]:
    profiles_path = Path(__file__).with_name('profiles')
    profile_path = profiles_path / 'iccup_dota_500_504_items.json'
    payload = json.loads(profile_path.read_text(encoding='utf-8'))
    names_path = profiles_path / 'iccup_dota_500_504_item_names.json'
    try:
        name_payload = json.loads(names_path.read_text(encoding='utf-8'))
        name_items = name_payload.get('items', {})
    except (OSError, json.JSONDecodeError):
        name_items = {}
    return {rawcode: ItemDefinition(rawcode=rawcode, cost=int(values.get('cost', 0)), name=canonical_item_name(name_items.get(rawcode, {}).get('name') or values.get('name')), name_confidence=name_items.get(rawcode, {}).get('confidence') if rawcode in name_items else 1.0 if values.get('name') else None, name_source=name_items.get(rawcode, {}).get('source') if rawcode in name_items else 'embedded confirmed mapping' if values.get('name') else None) for rawcode, values in payload['items'].items()}

def get_item_definition(rawcode: str | None) -> ItemDefinition | None:
    if rawcode is None:
        return None
    return load_item_profile().get(rawcode)
