from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
SETTINGS_SCHEMA_VERSION = 3
SERVICE_REPLAY_DIRECTORIES = frozenset({'000_replaylab', 'autosaved', 'replaylab'})
SERVICE_REPLAY_FILENAMES = frozenset({'lastreplay.w3g'})
TRANSIENT_PREFIXES = ('runtime/', 'session/', 'errors/', 'launch/runtime/', 'seeker/runtime/', 'camera/runtime/')
TRANSIENT_SEGMENTS = frozenset({'address', 'attach_error', 'attach_result', 'busy', 'error', 'exception', 'failure', 'last_error', 'pending', 'pid', 'process_id', 'replay_block', 'retry', 'status'})

class SettingsLike(Protocol):

    def allKeys(self) -> list[str]:
        ...

    def value(self, key: str, default: object=...) -> object:
        ...

    def setValue(self, key: str, value: object) -> None:
        ...

    def remove(self, key: str) -> None:
        ...

    def sync(self) -> None:
        ...

@dataclass(frozen=True)
class SettingsRecovery:
    removed_transient_keys: tuple[str, ...]
    removed_invalid_paths: tuple[str, ...]

    @property
    def repaired(self) -> bool:
        return bool(self.removed_transient_keys or self.removed_invalid_paths)

def _is_transient_key(key: str) -> bool:
    normalized = key.replace('\\', '/').strip('/').casefold()
    if any((normalized.startswith(prefix) for prefix in TRANSIENT_PREFIXES)):
        return True
    segments = tuple((part for part in normalized.split('/') if part))
    return any((segment in TRANSIENT_SEGMENTS for segment in segments))

def _stored_paths(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []

def _resolved_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)

def discover_replays(root: str | Path) -> tuple[Path, ...]:
    resolved_root = _resolved_path(root)
    if not resolved_root.is_dir():
        return ()
    replays: list[Path] = []
    for directory, child_directories, filenames in os.walk(resolved_root):
        child_directories[:] = [name for name in child_directories if name.casefold() not in SERVICE_REPLAY_DIRECTORIES]
        current = Path(directory)
        for filename in filenames:
            if Path(filename).suffix.casefold() != '.w3g':
                continue
            if filename.casefold() in SERVICE_REPLAY_FILENAMES:
                continue
            replay = current / filename
            if replay.is_file():
                replays.append(replay.resolve())

    def modified_time(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0
    replays.sort(key=lambda path: (modified_time(path), str(path).casefold()), reverse=True)
    return tuple(replays)

def recover_persistent_settings(settings: SettingsLike) -> SettingsRecovery:
    removed_transient: list[str] = []
    for key in tuple((str(item) for item in settings.allKeys())):
        if _is_transient_key(key):
            settings.remove(key)
            removed_transient.append(key)
    removed_paths: list[str] = []
    replay_paths: list[str] = []
    for stored_path in _stored_paths(settings.value('replay_library', [])):
        path = Path(stored_path)
        if path.is_file() and path.suffix.casefold() == '.w3g':
            resolved = str(path.resolve())
            if resolved not in replay_paths:
                replay_paths.append(resolved)
        else:
            removed_paths.append(stored_path)
    settings.setValue('replay_library', replay_paths)
    replay_roots: list[str] = []
    for stored_root in _stored_paths(settings.value('replay_roots', [])):
        if not stored_root.strip():
            continue
        resolved = str(_resolved_path(stored_root))
        if resolved not in replay_roots:
            replay_roots.append(resolved)
    if not replay_roots:
        last_directory = str(settings.value('last_directory', '') or '').strip()
        if last_directory and Path(last_directory).is_dir():
            replay_roots.append(str(_resolved_path(last_directory)))
    settings.setValue('replay_roots', replay_roots)
    last_replay = str(settings.value('last_replay', '') or '')
    if last_replay:
        path = Path(last_replay)
        if not path.is_file() or path.suffix.casefold() != '.w3g':
            settings.remove('last_replay')
            removed_paths.append(last_replay)
    if str(settings.value('seek_profile', 'balanced')) not in {'gentle', 'balanced', 'turbo'}:
        settings.setValue('seek_profile', 'balanced')
    settings.setValue('settings_schema_version', SETTINGS_SCHEMA_VERSION)
    settings.sync()
    return SettingsRecovery(removed_transient_keys=tuple(sorted(removed_transient)), removed_invalid_paths=tuple(removed_paths))

def forget_failed_replay(settings: SettingsLike, replay_path: str | Path | None) -> bool:
    if replay_path is None:
        return False
    stored = str(settings.value('last_replay', '') or '')
    if not stored:
        return False
    try:
        matches = Path(stored).resolve() == Path(replay_path).resolve()
    except OSError:
        matches = stored.casefold() == str(replay_path).casefold()
    if not matches:
        return False
    settings.remove('last_replay')
    settings.sync()
    return True
