from __future__ import annotations
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from .deep_analysis import DEEP_ANALYSIS_SCHEMA_VERSION, DeepAnalysisBundle, DeepAnalysisIdentity, EarnedGoldPoint, EarnedGoldTimeline, HeroXpPoint, HeroXpTimeline, NetWorthPoint, NetWorthTimeline
from .native_runtime import native_binary_candidates
from .native_runtime_host import native_file_sha256
SIDECAR_SCHEMA = 'replaylab-deep-analysis-v3'

def sha256_file(path: Path) -> str:
    return native_file_sha256(path).lower()

def deep_analysis_cache_directory() -> Path:
    local_app_data = os.environ.get('LOCALAPPDATA')
    if local_app_data:
        return Path(local_app_data) / 'ReplayLab' / 'deep-analysis'
    return Path.home() / 'AppData' / 'Local' / 'ReplayLab' / 'deep-analysis'

def find_deep_analysis_core() -> Path:
    for candidate in native_binary_candidates('replaylab_deep_analysis_core.exe', environment_variable='REPLAYLAB_DEEP_ANALYSIS_CORE', build_subdirectory='deep_analysis'):
        if candidate.is_file():
            return candidate.resolve()
    raise ValueError('Deep Analysis Core was not found. Reinstall ReplayLab.')

def _slot_rows(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError('Deep Analysis series must be arrays')
    if not all((isinstance(row, dict) for row in raw)):
        raise ValueError('Deep Analysis series row is invalid')
    return list(raw)

def _integer(row: dict[str, Any], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f'Deep Analysis snapshot field {key} is invalid')
    return value

def bundle_to_dict(bundle: DeepAnalysisBundle) -> dict[str, Any]:
    return {'schema': SIDECAR_SCHEMA, 'schema_version': bundle.schema_version, 'identity': {'replay_sha256': bundle.identity.replay_sha256, 'game_dll_sha256': bundle.identity.game_dll_sha256, 'analysis_profile_id': bundle.identity.analysis_profile_id, 'ledger_contract_id': bundle.identity.ledger_contract_id, 'cache_key': bundle.identity.cache_key}, 'duration_ms': bundle.duration_ms, 'series': {'gpm': [{'player_slot': slot, 'source': timeline.source, 'confidence': timeline.confidence, 'issues': list(timeline.issues), 'points': [{'game_time_ms': point.game_time_ms, 'earned_gold_tenths': point.earned_gold_tenths} for point in timeline.points]} for slot, timeline in sorted(bundle.earned_gold_timelines.items())], 'xpm': [{'player_slot': slot, 'source': timeline.source, 'confidence': timeline.confidence, 'issues': list(timeline.issues), 'points': [{'game_time_ms': point.game_time_ms, 'xp': point.xp} for point in timeline.points]} for slot, timeline in sorted(bundle.hero_xp_timelines.items())], 'net_worth': [{'player_slot': slot, 'source': timeline.source, 'confidence': timeline.confidence, 'containers': list(timeline.containers), 'issues': list(timeline.issues), 'points': [{'game_time_ms': point.game_time_ms, 'wallet': point.wallet, 'owned_item_value': point.owned_item_value} for point in timeline.points]} for slot, timeline in sorted(bundle.net_worth_timelines.items())]}}

def bundle_from_dict(payload: object) -> DeepAnalysisBundle:
    if not isinstance(payload, dict):
        raise ValueError('Deep Analysis Core returned an invalid snapshot')
    identity_row = payload.get('identity')
    series = payload.get('series')
    if not isinstance(identity_row, dict) or not isinstance(series, dict):
        raise ValueError('Deep Analysis Core snapshot is incomplete')
    identity = DeepAnalysisIdentity(replay_sha256=str(identity_row['replay_sha256']), game_dll_sha256=str(identity_row['game_dll_sha256']), analysis_profile_id=str(identity_row['analysis_profile_id']), ledger_contract_id=str(identity_row['ledger_contract_id']), cache_key_digest=str(identity_row['cache_key']))
    earned: dict[int, EarnedGoldTimeline] = {}
    for row in _slot_rows(series.get('gpm')):
        slot = _integer(row, 'player_slot')
        earned[slot] = EarnedGoldTimeline(player_slot=slot, points=tuple((EarnedGoldPoint(slot, _integer(point, 'game_time_ms'), _integer(point, 'earned_gold_tenths')) for point in _slot_rows(row.get('points')))), issues=tuple((str(issue) for issue in row.get('issues', []))), source=str(row['source']), confidence=str(row['confidence']))
    xp: dict[int, HeroXpTimeline] = {}
    for row in _slot_rows(series.get('xpm')):
        slot = _integer(row, 'player_slot')
        xp[slot] = HeroXpTimeline(player_slot=slot, points=tuple((HeroXpPoint(slot, _integer(point, 'game_time_ms'), _integer(point, 'xp')) for point in _slot_rows(row.get('points')))), issues=tuple((str(issue) for issue in row.get('issues', []))), source=str(row['source']), confidence=str(row['confidence']))
    net_worth: dict[int, NetWorthTimeline] = {}
    for row in _slot_rows(series.get('net_worth')):
        slot = _integer(row, 'player_slot')
        net_worth[slot] = NetWorthTimeline(player_slot=slot, points=tuple((NetWorthPoint(slot, _integer(point, 'game_time_ms'), _integer(point, 'wallet'), _integer(point, 'owned_item_value')) for point in _slot_rows(row.get('points')))), issues=tuple((str(issue) for issue in row.get('issues', []))), containers=tuple((str(value) for value in row.get('containers', []))), source=str(row['source']), confidence=str(row['confidence']))
    bundle = DeepAnalysisBundle(schema_version=_integer(payload, 'schema_version'), identity=identity, duration_ms=_integer(payload, 'duration_ms'), wallet_ledgers={}, hero_xp_timelines=xp, earned_gold_timelines=earned, net_worth_timelines=net_worth)
    if not bundle.is_valid:
        raise ValueError('Deep Analysis Core returned no valid series')
    return bundle

def bundle_from_json(path: Path) -> DeepAnalysisBundle:
    process = subprocess.run([str(find_deep_analysis_core()), str(path.resolve())], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='strict', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if process.returncode != 0:
        detail = process.stderr.strip() or 'Deep Analysis sidecar was rejected'
        raise ValueError(detail)
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError('Deep Analysis Core returned invalid JSON') from exc
    return bundle_from_dict(payload)

class DeepAnalysisCache:

    def __init__(self, root: Path | None=None) -> None:
        self.root = (root or deep_analysis_cache_directory()).resolve(strict=False)

    def path_for(self, identity: DeepAnalysisIdentity) -> Path:
        return self.root / f'{identity.cache_key}.json'

    def store(self, bundle: DeepAnalysisBundle) -> Path:
        if not bundle.is_valid:
            raise ValueError('invalid Deep Analysis bundle is not cacheable')
        self.root.mkdir(parents=True, exist_ok=True)
        destination = self.path_for(bundle.identity)
        pending = destination.with_suffix('.json.pending')
        pending.write_text(json.dumps(bundle_to_dict(bundle), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(pending, destination)
        return destination

    def load(self, identity: DeepAnalysisIdentity) -> DeepAnalysisBundle | None:
        path = self.path_for(identity)
        if not path.is_file():
            return None
        bundle = bundle_from_json(path)
        if bundle.identity != identity:
            raise ValueError('cached Deep Analysis identity mismatch')
        return bundle

    def latest_for_replay(self, replay_sha256: str, analysis_profile_id: str | None=None) -> DeepAnalysisBundle | None:
        if not self.root.is_dir():
            return None
        candidates: list[tuple[float, DeepAnalysisBundle]] = []
        for path in self.root.glob('*.json'):
            try:
                bundle = bundle_from_json(path)
            except (OSError, ValueError):
                continue
            if bundle.identity.replay_sha256.lower() != replay_sha256.lower():
                continue
            if analysis_profile_id is not None and bundle.identity.analysis_profile_id != analysis_profile_id:
                continue
            candidates.append((path.stat().st_mtime, bundle))
        return max(candidates, key=lambda item: item[0])[1] if candidates else None
