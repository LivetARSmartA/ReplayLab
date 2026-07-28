from __future__ import annotations
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
MAP_RESOLVER_PROTOCOL_VERSION = 1
_SHA256 = re.compile('[0-9a-fA-F]{64}')

class MapResolverError(ValueError):
    pass

def normalize_map_path(value: str) -> str:
    path = PureWindowsPath(value.replace('/', '\\').strip())
    if not path.parts or path.is_absolute() or path.drive or any((part in {'', '.', '..'} for part in path.parts)) or (path.suffix.casefold() not in {'.w3x', '.w3m'}):
        raise MapResolverError(f'Unsafe Warcraft map path: {value!r}')
    return str(path)

@dataclass(frozen=True)
class MapRequirement:
    logical_path: str
    map_checksum: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'logical_path', normalize_map_path(self.logical_path))
        if self.map_checksum is not None and _SHA256.fullmatch(self.map_checksum) is None:
            raise MapResolverError('Invalid required map SHA-256')
        if self.map_checksum is not None:
            object.__setattr__(self, 'map_checksum', self.map_checksum.casefold())

@dataclass(frozen=True)
class MapArtifact:
    map_id: str
    logical_path: str
    sha256: str
    size_bytes: int
    download_url: str

    def __post_init__(self) -> None:
        object.__setattr__(self, 'logical_path', normalize_map_path(self.logical_path))
        if not self.map_id or len(self.map_id) > 160:
            raise MapResolverError('Invalid map artifact id')
        if _SHA256.fullmatch(self.sha256) is None:
            raise MapResolverError('Invalid map SHA-256')
        if self.size_bytes <= 0:
            raise MapResolverError('Invalid map artifact size')
        if not self.download_url.casefold().startswith('https://'):
            raise MapResolverError('Map downloads require HTTPS')

    def cache_path(self, cache_root: str | Path) -> Path:
        suffix = PureWindowsPath(self.logical_path).suffix.casefold()
        return Path(cache_root) / self.sha256[:2].casefold() / f'{self.sha256.casefold()}{suffix}'

def local_map_path(warcraft_root: str | Path, requirement: MapRequirement) -> Path:
    relative = Path(*PureWindowsPath(requirement.logical_path).parts)
    return Path(warcraft_root) / relative
