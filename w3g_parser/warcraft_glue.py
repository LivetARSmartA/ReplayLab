from __future__ import annotations
import ctypes
from dataclasses import dataclass
from pathlib import Path
from .native_runtime_host import NativeRuntimeError, NativeRuntimeHost

class WarcraftGlueError(RuntimeError):
    pass

@dataclass(frozen=True)
class MapListState:
    current_directory: str
    selected_index: int
    selection_ready: bool

class WarcraftGlueChannel:
    WM_KEYDOWN = 256
    WM_KEYUP = 257
    CMAIN_MENU = b'CMainMenu.h'
    CSINGLE_PLAYER_MENU = b'CSinglePlayerMenu.h'
    CVIEW_REPLAY_SCREEN = b'CViewReplayScreen.h'
    CMAP_LIST = b'CMapList.h'

    def __init__(self, _kernel32: object, user32: object, pid: int, window_handle: int, game: Path) -> None:
        self._user32 = user32
        self._window = window_handle
        self._host = NativeRuntimeHost()
        try:
            self._host.exchange('configure_glue', {'process_id': pid, 'replay_root': str((game.parent / 'Replay').resolve())})
        except NativeRuntimeError as exc:
            self._host.close()
            raise WarcraftGlueError(str(exc)) from exc

    def __enter__(self) -> WarcraftGlueChannel:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self._host.close()

    def private_signatures(self, patterns: set[bytes]) -> set[bytes]:
        try:
            encoded = [pattern.decode('ascii') for pattern in patterns]
        except UnicodeDecodeError as exc:
            raise WarcraftGlueError('Warcraft UI signatures must be ASCII') from exc
        try:
            response = self._host.exchange('private_signatures', {'patterns': encoded})
        except NativeRuntimeError as exc:
            raise WarcraftGlueError(str(exc)) from exc
        raw_found = response.get('found')
        if not isinstance(raw_found, list):
            raise WarcraftGlueError('Runtime Engine returned invalid UI state')
        return {str(value).encode('ascii') for value in raw_found}

    def map_list_state(self) -> MapListState | None:
        try:
            response = self._host.exchange('map_list')
        except NativeRuntimeError as exc:
            raise WarcraftGlueError(str(exc)) from exc
        raw_state = response.get('map_list')
        if raw_state is None:
            return None
        if not isinstance(raw_state, dict):
            raise WarcraftGlueError('Runtime Engine returned invalid map list')
        return MapListState(current_directory=str(raw_state['current_directory']), selected_index=int(raw_state['selected_index']), selection_ready=bool(raw_state['selection_ready']))

    def read_text_tail(self, path: Path, offset: int) -> str:
        try:
            response = self._host.exchange('read_text_tail', {'path': str(path.resolve()), 'offset': offset})
        except NativeRuntimeError as exc:
            raise WarcraftGlueError(str(exc)) from exc
        text = response.get('text')
        if not isinstance(text, str):
            raise WarcraftGlueError('Runtime Engine returned an invalid text response')
        return text

    def post_key(self, virtual_key: int, scan_code: int, *, extended: bool=False) -> None:
        flags = 1 | scan_code << 16
        if extended:
            flags |= 1 << 24
        if not self._user32.PostMessageW(self._window, self.WM_KEYDOWN, virtual_key, flags):
            error = ctypes.get_last_error()
            raise WarcraftGlueError(f'Warcraft rejected WM_KEYDOWN (WinError {error}).')
        if not self._user32.PostMessageW(self._window, self.WM_KEYUP, virtual_key, flags | 3221225472):
            error = ctypes.get_last_error()
            raise WarcraftGlueError(f'Warcraft rejected WM_KEYUP (WinError {error}).')
