from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from .assets import release_build_id
INTEGRATION_PROTOCOL_VERSION = 1
INTEGRATION_CAPABILITIES = ('open-replay', 'verified-launch', 'instant-seek', 'camera-control', 'map-resolver-v1')

class IntegrationContractError(ValueError):
    pass

@dataclass(frozen=True)
class OpenReplayRequest:
    request_id: str
    replay_path: Path
    warcraft_pid: int | None = None
    map_policy: str = 'local-only'

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> OpenReplayRequest:
        version = payload.get('protocol_version')
        if version != INTEGRATION_PROTOCOL_VERSION:
            raise IntegrationContractError(f'Unsupported integration protocol: {version!r}')
        if payload.get('action') != 'open-replay':
            raise IntegrationContractError('Unsupported integration action')
        request_id = str(payload.get('request_id', '')).strip()
        if not request_id or len(request_id) > 128:
            raise IntegrationContractError('Invalid request id')
        replay_path = Path(str(payload.get('replay_path', '')).strip())
        if replay_path.suffix.casefold() != '.w3g':
            raise IntegrationContractError('Replay path must end in .w3g')
        if not replay_path.is_absolute():
            raise IntegrationContractError('Replay path must be absolute')
        pid_value = payload.get('warcraft_pid')
        warcraft_pid = None
        if pid_value is not None:
            try:
                warcraft_pid = int(pid_value)
            except (TypeError, ValueError) as exc:
                raise IntegrationContractError('Warcraft pid must be an integer') from exc
            if warcraft_pid <= 0:
                raise IntegrationContractError('Warcraft pid must be positive')
        map_policy = str(payload.get('map_policy', 'local-only'))
        if map_policy not in {'local-only', 'allow-download'}:
            raise IntegrationContractError('Unsupported map policy')
        return cls(request_id=request_id, replay_path=replay_path, warcraft_pid=warcraft_pid, map_policy=map_policy)

def integration_manifest() -> dict[str, object]:
    return {'product': 'ReplayLab', 'build_id': release_build_id() or 'development', 'protocol_version': INTEGRATION_PROTOCOL_VERSION, 'transport': 'local-versioned-ipc', 'capabilities': list(INTEGRATION_CAPABILITIES), 'security': {'scope': 'current-user', 'replay_only': True, 'arbitrary_memory_write': False}}
