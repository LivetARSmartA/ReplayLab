from __future__ import annotations
import ctypes
import struct
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

class WarcraftGlueError(RuntimeError):
    pass

@dataclass(frozen=True)
class MapListState:
    region_base: int
    current_directory: str
    selected_index: int
    selected_pointer: int

class MemoryBasicInformation(ctypes.Structure):
    _fields_ = [('BaseAddress', ctypes.c_void_p), ('AllocationBase', ctypes.c_void_p), ('AllocationProtect', wintypes.DWORD), ('RegionSize', ctypes.c_size_t), ('State', wintypes.DWORD), ('Protect', wintypes.DWORD), ('Type', wintypes.DWORD)]

class WarcraftGlueChannel:
    PROCESS_QUERY_INFORMATION = 1024
    PROCESS_VM_READ = 16
    MEM_COMMIT = 4096
    MEM_PRIVATE = 131072
    PAGE_GUARD = 256
    PAGE_NOACCESS = 1
    MAX_32BIT_ADDRESS = 2147418112
    WM_KEYDOWN = 256
    WM_KEYUP = 257
    CMAIN_MENU = b'CMainMenu.h'
    CSINGLE_PLAYER_MENU = b'CSinglePlayerMenu.h'
    CVIEW_REPLAY_SCREEN = b'CViewReplayScreen.h'
    CMAP_LIST = b'CMapList.h'
    MAP_LIST_SELECTED_POINTER_OFFSET = 676
    MAP_LIST_SELECTED_INDEX_OFFSET = 680
    MAP_LIST_RELATIVE_PATH_OFFSET = 756
    MAP_LIST_ABSOLUTE_PATH_OFFSET = 1016
    MAP_LIST_CURRENT_DIRECTORY_OFFSET = 1276
    MAP_LIST_PROBE_SIZE = 1408

    def __init__(self, kernel32: object, user32: object, pid: int, window_handle: int, game: Path) -> None:
        self._kernel32 = kernel32
        self._user32 = user32
        self._pid = pid
        self._window = window_handle
        self._game = game
        self._handle = self._kernel32.OpenProcess(self.PROCESS_QUERY_INFORMATION | self.PROCESS_VM_READ, False, self._pid)
        if not self._handle:
            error = ctypes.get_last_error()
            raise WarcraftGlueError(f'Не удалось открыть процесс Warcraft для read-only проверки экранов (WinError {error}).')

    def __enter__(self) -> WarcraftGlueChannel:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None

    @staticmethod
    def _cstring(block: bytes, offset: int, limit: int=260) -> str:
        payload = block[offset:offset + limit].split(b'\x00', 1)[0]
        return payload.decode('mbcs', errors='strict')

    def _private_blocks(self) -> Iterator[tuple[int, bytes]]:
        if not self._handle:
            raise WarcraftGlueError('Read-only канал Warcraft уже закрыт.')
        address = 0
        while address < self.MAX_32BIT_ADDRESS:
            info = MemoryBasicInformation()
            queried = self._kernel32.VirtualQueryEx(self._handle, ctypes.c_void_p(address), ctypes.byref(info), ctypes.sizeof(info))
            if not queried:
                break
            base = int(info.BaseAddress or address)
            size = int(info.RegionSize)
            readable = info.State == self.MEM_COMMIT and info.Type == self.MEM_PRIVATE and (not info.Protect & self.PAGE_GUARD) and (info.Protect & 255 not in (0, self.PAGE_NOACCESS)) and (0 < size <= 64 * 1024 * 1024)
            if readable:
                buffer = ctypes.create_string_buffer(size)
                read = ctypes.c_size_t()
                if self._kernel32.ReadProcessMemory(self._handle, ctypes.c_void_p(base), buffer, size, ctypes.byref(read)):
                    yield (base, buffer.raw[:read.value])
            address = max(address + 4096, base + size)

    def private_signatures(self, patterns: set[bytes]) -> set[bytes]:
        missing = set(patterns)
        found: set[bytes] = set()
        for _, block in self._private_blocks():
            for pattern in tuple(missing):
                if pattern in block:
                    found.add(pattern)
                    missing.remove(pattern)
            if not missing:
                break
        return found

    @classmethod
    def parse_map_list_block(cls, base: int, block: bytes, expected_absolute: bytes) -> MapListState | None:
        if len(block) < cls.MAP_LIST_PROBE_SIZE or cls.CMAP_LIST not in block:
            return None
        try:
            relative = cls._cstring(block, cls.MAP_LIST_RELATIVE_PATH_OFFSET)
            absolute = cls._cstring(block, cls.MAP_LIST_ABSOLUTE_PATH_OFFSET).encode('mbcs')
            current = cls._cstring(block, cls.MAP_LIST_CURRENT_DIRECTORY_OFFSET)
        except UnicodeError:
            return None
        if relative != 'Replay\\' or absolute.lower() != expected_absolute.lower():
            return None
        selected_pointer, selected_index = struct.unpack_from('<II', block, cls.MAP_LIST_SELECTED_POINTER_OFFSET)
        if selected_index > 10000:
            return None
        return MapListState(region_base=base, current_directory=current, selected_index=selected_index, selected_pointer=selected_pointer)

    def map_list_state(self) -> MapListState | None:
        replay_root = (self._game.parent / 'Replay').resolve()
        expected_absolute = (str(replay_root).rstrip('\\/') + '\\').encode('mbcs')
        for base, block in self._private_blocks():
            state = self.parse_map_list_block(base, block, expected_absolute)
            if state is not None:
                return state
        return None

    def post_key(self, virtual_key: int, scan_code: int, *, extended: bool=False) -> None:
        flags = 1 | scan_code << 16
        if extended:
            flags |= 1 << 24
        if not self._user32.PostMessageW(self._window, self.WM_KEYDOWN, virtual_key, flags):
            error = ctypes.get_last_error()
            raise WarcraftGlueError(f'Warcraft отклонил WM_KEYDOWN (WinError {error}).')
        if not self._user32.PostMessageW(self._window, self.WM_KEYUP, virtual_key, flags | 3221225472):
            error = ctypes.get_last_error()
            raise WarcraftGlueError(f'Warcraft отклонил WM_KEYUP (WinError {error}).')
