from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from .native_runtime_host import NativeRuntimeError, NativeRuntimeHost

class WarcraftBuildError(ValueError):
    pass

@dataclass(frozen=True)
class GameDllMatch:
    path: Path
    sha256: str
    profile_key: str
    profile_label: str
    match_kind: str = 'exact'

    @property
    def exact(self) -> bool:
        return self.match_kind == 'exact'

def match_game_dll(path: str | Path) -> GameDllMatch:
    game_dll = Path(path).resolve()
    war3 = game_dll.with_name('war3.exe')
    try:
        with NativeRuntimeHost() as host:
            response = host.exchange('validate_build', {'war3_path': str(war3), 'game_dll_path': str(game_dll)})
    except NativeRuntimeError as exc:
        raise WarcraftBuildError(str(exc)) from exc
    return GameDllMatch(path=game_dll, sha256=str(response['sha256']), profile_key=str(response['profile_key']), profile_label=str(response['profile_label']))
