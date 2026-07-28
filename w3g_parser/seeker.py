from __future__ import annotations
import ctypes
import hashlib
import math
import os
import struct
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from .warcraft_build import GameDllMatch, WARCRAFT_126A_WAR3_SHA256, WarcraftBuildError, match_game_dll

class SeekBackendError(RuntimeError):
    pass

class SeekCancelled(SeekBackendError):
    pass

@dataclass(frozen=True)
class AttachResult:
    pid: int
    executable: str
    game_dll: str
    replay_block: int
    replay_position_ms: int
    replay_length_ms: int
    build_profile: str = ''
    game_dll_match: str = ''
    game_dll_sha256: str = ''

@dataclass(frozen=True)
class ProcessAttachResult:
    pid: int
    executable: str
    game_dll: str
    build_profile: str
    game_dll_match: str
    game_dll_sha256: str

@dataclass(frozen=True)
class SeekProgress:
    current_replay_time_ms: int
    target_replay_time_ms: int
    speed_value: int

@dataclass(frozen=True)
class CameraState:
    target_x: float
    target_y: float
    distance: float
    yaw: float
    pitch: float
    roll: float
    z_offset: float

@dataclass(frozen=True)
class CameraRuntimeSession:
    process_id: int
    target_x_address: int
    target_y_address: int
    distance_address: int
    yaw_address: int
    pitch_address: int
    roll_address: int
    z_offset_address: int
    initial_state: CameraState

@dataclass(frozen=True)
class SeekProfile:
    key: str
    label: str
    maximum_speed: int
    far_poll_seconds: float
    lower_process_priority: bool
SEEK_PROFILES = {'gentle': SeekProfile(key='gentle', label='Бережный · до 16x', maximum_speed=16, far_poll_seconds=0.16, lower_process_priority=True), 'balanced': SeekProfile(key='balanced', label='Сбалансированный · до 32x', maximum_speed=32, far_poll_seconds=0.1, lower_process_priority=True), 'turbo': SeekProfile(key='turbo', label='Турбо · максимум', maximum_speed=65535, far_poll_seconds=0.06, lower_process_priority=False)}

class Warcraft126MemoryBackend:
    EXPECTED_WAR3_SHA256 = WARCRAFT_126A_WAR3_SHA256
    REPLAY_LENGTH_OFFSET = 2308
    REPLAY_POSITION_OFFSET = 7456
    REPLAY_SPEED_OFFSET = 9060
    REPLAY_SPEED_DIVIDER_OFFSET = 9064
    PAUSE_OFFSET = 9068
    STATUS_CODE_OFFSET = 9016
    TEMP_REPLAY_PATH_OFFSET = 3484
    STATUS_LOOP = 1280266064
    PROCESS_QUERY_INFORMATION = 1024
    PROCESS_QUERY_LIMITED_INFORMATION = 4096
    PROCESS_SET_INFORMATION = 512
    PROCESS_VM_OPERATION = 8
    PROCESS_VM_READ = 16
    PROCESS_VM_WRITE = 32
    BELOW_NORMAL_PRIORITY_CLASS = 16384
    TH32CS_SNAPPROCESS = 2
    TH32CS_SNAPMODULE = 8
    TH32CS_SNAPMODULE32 = 16
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    MEM_COMMIT = 4096
    PAGE_GUARD = 256
    PAGE_NOACCESS = 1
    MAX_32BIT_ADDRESS = 2147418112
    CAMERA_TARGET_X_OFFSET = 432
    CAMERA_TARGET_Y_OFFSET = 436
    CAMERA_DISTANCE_OFFSET = 832
    CAMERA_DISTANCE_MAX_OFFSET = 844
    CAMERA_VIEW_MAX_OFFSET = 972
    CAMERA_VIEW_MAX_MIRROR_OFFSET = 984
    CAMERA_FOV_OFFSET = 1252
    CAMERA_ROLL_OFFSET = 1392
    CAMERA_YAW_OFFSET = 1532
    CAMERA_PITCH_OFFSET = 1672
    CAMERA_Z_OFFSET = 1812
    CAMERA_POSITION_POINTER_OFFSET = 1868
    SELECTED_UNIT_SIGNATURE = 1767994469
    SELECTED_UNIT_SIGNATURE_OFFSET = 164
    SELECTED_UNIT_POINTER_OFFSET = 476
    UNIT_RUNTIME_POINTER_OFFSET = 8
    UNIT_HANDLE_ID_OFFSET = 12
    UNIT_RUNTIME_ID_OFFSET = 16
    UNIT_RAWCODE_OFFSET = 48
    UNIT_OWNER_OFFSET = 88
    UNIT_X_OFFSET = 644
    UNIT_Y_OFFSET = 648
    UNIT_VTABLE_OFFSET = 9640244

    def __init__(self) -> None:
        if os.name != 'nt':
            raise SeekBackendError('The live seeker backend requires Windows')
        self._kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self._handle: int | None = None
        self._pid: int | None = None
        self._process_path: Path | None = None
        self._game_dll_base: int | None = None
        self._game_dll_match: GameDllMatch | None = None
        self._replay_block: int | None = None
        self._camera_position_block: int | None = None
        self._camera_other_block: int | None = None
        self._selected_unit_pointer: int | None = None
        self._hero_cache: dict[tuple[int, str], int] = {}
        self._hero_cache_lock = threading.Lock()
        self._lock = threading.RLock()
        self._configure_winapi()

    def _configure_winapi(self) -> None:
        kernel32 = self._kernel32
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPVOID, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        kernel32.ReadProcessMemory.restype = wintypes.BOOL
        kernel32.WriteProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.LPCVOID, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        kernel32.WriteProcessMemory.restype = wintypes.BOOL
        kernel32.VirtualQueryEx.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPVOID, ctypes.c_size_t]
        kernel32.VirtualQueryEx.restype = ctypes.c_size_t
        kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.GetPriorityClass.argtypes = [wintypes.HANDLE]
        kernel32.GetPriorityClass.restype = wintypes.DWORD
        kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.SetPriorityClass.restype = wintypes.BOOL

    @property
    def attached(self) -> bool:
        return self._handle is not None and self._replay_block is not None

    @property
    def process_attached(self) -> bool:
        return self._handle is not None and self._game_dll_base is not None

    @property
    def replay_block(self) -> int:
        if self._replay_block is None:
            raise SeekBackendError('Warcraft replay is not attached')
        return self._replay_block

    @property
    def process_id(self) -> int:
        if self._pid is None:
            raise SeekBackendError('Warcraft process is not open')
        return self._pid

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest().upper()

    def _open_process(self, pid: int, access: int) -> int:
        handle = self._kernel32.OpenProcess(access, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            raise SeekBackendError(f'Cannot open war3.exe process {pid} (WinError {error})')
        return int(handle)

    def _query_process_path(self, pid: int) -> Path:
        handle = self._open_process(pid, self.PROCESS_QUERY_LIMITED_INFORMATION)
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not self._kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                error = ctypes.get_last_error()
                raise SeekBackendError(f'Cannot resolve war3.exe path (WinError {error})')
            return Path(buffer.value)
        finally:
            self._kernel32.CloseHandle(handle)

    def _find_war3_processes(self) -> list[tuple[int, str]]:

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD), ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG), ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]
        create_snapshot = self._kernel32.CreateToolhelp32Snapshot
        create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        create_snapshot.restype = wintypes.HANDLE
        process_first = self._kernel32.Process32FirstW
        process_first.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        process_first.restype = wintypes.BOOL
        process_next = self._kernel32.Process32NextW
        process_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        process_next.restype = wintypes.BOOL
        snapshot = create_snapshot(self.TH32CS_SNAPPROCESS, 0)
        if int(snapshot) == self.INVALID_HANDLE_VALUE:
            raise SeekBackendError('Cannot enumerate Windows processes')
        matches: list[tuple[int, str]] = []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            ok = process_first(snapshot, ctypes.byref(entry))
            while ok:
                name = entry.szExeFile
                if name.lower() in {'war3.exe', 'warcraft iii.exe'}:
                    matches.append((int(entry.th32ProcessID), name))
                ok = process_next(snapshot, ctypes.byref(entry))
        finally:
            self._kernel32.CloseHandle(snapshot)
        return matches

    def _find_module_base(self, pid: int, module_name: str) -> int:

        class MODULEENTRY32W(ctypes.Structure):
            _fields_ = [('dwSize', wintypes.DWORD), ('th32ModuleID', wintypes.DWORD), ('th32ProcessID', wintypes.DWORD), ('GlblcntUsage', wintypes.DWORD), ('ProccntUsage', wintypes.DWORD), ('modBaseAddr', ctypes.POINTER(ctypes.c_byte)), ('modBaseSize', wintypes.DWORD), ('hModule', wintypes.HMODULE), ('szModule', wintypes.WCHAR * 256), ('szExePath', wintypes.WCHAR * 260)]
        create_snapshot = self._kernel32.CreateToolhelp32Snapshot
        create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        create_snapshot.restype = wintypes.HANDLE
        module_first = self._kernel32.Module32FirstW
        module_first.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
        module_first.restype = wintypes.BOOL
        module_next = self._kernel32.Module32NextW
        module_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
        module_next.restype = wintypes.BOOL
        snapshot = create_snapshot(self.TH32CS_SNAPMODULE | self.TH32CS_SNAPMODULE32, pid)
        if int(snapshot) == self.INVALID_HANDLE_VALUE:
            raise SeekBackendError('Cannot enumerate Warcraft modules')
        try:
            entry = MODULEENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            ok = module_first(snapshot, ctypes.byref(entry))
            while ok:
                if entry.szModule.lower() == module_name.lower():
                    address = ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value
                    if address:
                        return int(address)
                ok = module_next(snapshot, ctypes.byref(entry))
        finally:
            self._kernel32.CloseHandle(snapshot)
        raise SeekBackendError(f'{module_name} is not loaded in Warcraft')

    def _validate_binaries(self, process_path: Path) -> tuple[Path, GameDllMatch]:
        game_dll = process_path.with_name('Game.dll')
        if not game_dll.is_file():
            raise SeekBackendError(f'Game.dll was not found next to {process_path}')
        war3_hash = self._sha256(process_path)
        if war3_hash != self.EXPECTED_WAR3_SHA256:
            raise SeekBackendError('war3.exe does not match the locked Warcraft 1.26a build')
        try:
            game_match = match_game_dll(game_dll)
        except (OSError, WarcraftBuildError) as exc:
            raise SeekBackendError(str(exc)) from exc
        return (game_dll, game_match)

    def _read(self, address: int, size: int) -> bytes:
        if self._handle is None:
            raise SeekBackendError('Warcraft process is not open')
        if size <= 0:
            return b''
        buffer = ctypes.create_string_buffer(size)
        read = ctypes.c_size_t()
        ok = self._kernel32.ReadProcessMemory(self._handle, ctypes.c_void_p(address), buffer, size, ctypes.byref(read))
        if not ok or read.value != size:
            error = ctypes.get_last_error()
            raise SeekBackendError(f'Cannot read Warcraft memory at 0x{address:08X} (WinError {error})')
        return buffer.raw

    def _try_read(self, address: int, size: int) -> bytes | None:
        try:
            return self._read(address, size)
        except SeekBackendError:
            return None

    def _read_i32(self, offset: int) -> int:
        return struct.unpack('<i', self._read(self.replay_block + offset, 4))[0]

    def _read_u32(self, offset: int) -> int:
        return struct.unpack('<I', self._read(self.replay_block + offset, 4))[0]

    def _write_i32(self, offset: int, value: int) -> None:
        if self._handle is None:
            raise SeekBackendError('Warcraft process is not open')
        payload = struct.pack('<i', value)
        written = ctypes.c_size_t()
        address = self.replay_block + offset
        ok = self._kernel32.WriteProcessMemory(self._handle, ctypes.c_void_p(address), payload, len(payload), ctypes.byref(written))
        if not ok or written.value != len(payload):
            error = ctypes.get_last_error()
            raise SeekBackendError(f'Cannot write Warcraft memory at 0x{address:08X} (WinError {error})')
        if self._read(address, 4) != payload:
            raise SeekBackendError(f'Warcraft rejected the write at 0x{address:08X}')

    def _read_abs_i32(self, address: int) -> int:
        return struct.unpack('<i', self._read(address, 4))[0]

    def _read_abs_f32(self, address: int) -> float:
        return struct.unpack('<f', self._read(address, 4))[0]

    def _write_abs_f32(self, address: int, value: float, *, verify: bool=False) -> None:
        if self._handle is None:
            raise SeekBackendError('Warcraft process is not open')
        if not math.isfinite(value):
            raise ValueError('Camera value must be finite')
        payload = struct.pack('<f', float(value))
        written = ctypes.c_size_t()
        ok = self._kernel32.WriteProcessMemory(self._handle, ctypes.c_void_p(address), payload, len(payload), ctypes.byref(written))
        if not ok or written.value != len(payload):
            error = ctypes.get_last_error()
            raise SeekBackendError(f'Cannot write Warcraft camera at 0x{address:08X} (WinError {error})')
        if verify and self._read(address, 4) != payload:
            raise SeekBackendError(f'Warcraft rejected the camera write at 0x{address:08X}')

    def _camera_candidate(self, other_block: int) -> tuple[int, int] | None:
        pointer = self._try_read(other_block + self.CAMERA_POSITION_POINTER_OFFSET, 4)
        if pointer is None:
            return None
        position_block = struct.unpack('<I', pointer)[0] & 4294901760
        if position_block < 65536:
            return None
        addresses = [position_block + self.CAMERA_TARGET_X_OFFSET, position_block + self.CAMERA_TARGET_Y_OFFSET, other_block + self.CAMERA_DISTANCE_OFFSET, other_block + self.CAMERA_DISTANCE_MAX_OFFSET, other_block + self.CAMERA_VIEW_MAX_OFFSET, other_block + self.CAMERA_FOV_OFFSET, other_block + self.CAMERA_YAW_OFFSET, other_block + self.CAMERA_PITCH_OFFSET]
        values: list[float] = []
        for address in addresses:
            payload = self._try_read(address, 4)
            if payload is None:
                return None
            value = struct.unpack('<f', payload)[0]
            if not math.isfinite(value):
                return None
            values.append(value)
        target_x, target_y, distance, distance_max, view_max, fov, yaw, pitch = values
        if not (abs(target_x) <= 100000 and abs(target_y) <= 100000 and (20 <= distance <= 1000000) and (20 <= distance_max <= 2000000) and (20 <= view_max <= 2000000) and (0.01 <= fov <= 10) and (abs(yaw) <= 64) and (abs(pitch) <= 64)):
            return None
        return (position_block, other_block)

    def attach_camera(self, cancel: threading.Event | None=None) -> CameraState:
        with self._lock:
            if not self.process_attached:
                raise SeekBackendError('Сначала подключись к процессу Warcraft.')
            camera_blocks: tuple[int, int] | None = None
            selected_unit_pointer: int | None = None
            for index in range(12289):
                if cancel is not None and cancel.is_set():
                    raise SeekCancelled('Camera scan was cancelled')
                base = index << 16
                if selected_unit_pointer is None:
                    signature = self._try_read(base + self.SELECTED_UNIT_SIGNATURE_OFFSET, 4)
                    if signature is not None and struct.unpack('<i', signature)[0] == self.SELECTED_UNIT_SIGNATURE:
                        selected_unit_pointer = base + self.SELECTED_UNIT_POINTER_OFFSET
                if camera_blocks is None:
                    probes = ((base, 736, 1080), (base + 9472, 876, 1080), (base, 876, 1080), (base + 9536, 876, 1148))
                    for other_block, height_offset, expected_height in probes:
                        payload = self._try_read(other_block + height_offset, 4)
                        if payload is None:
                            continue
                        if struct.unpack('<i', payload)[0] != expected_height:
                            continue
                        camera_blocks = self._camera_candidate(other_block)
                        if camera_blocks is not None:
                            break
                if camera_blocks is not None and selected_unit_pointer is not None:
                    break
            if camera_blocks is None:
                raise SeekBackendError('Камера Warcraft 1.26 не найдена в памяти.')
            self._camera_position_block, self._camera_other_block = camera_blocks
            self._selected_unit_pointer = selected_unit_pointer
            return self.camera_state()

    def camera_state(self) -> CameraState:
        with self._lock:
            if self._camera_position_block is None or self._camera_other_block is None:
                raise SeekBackendError('Камера ещё не подключена')
            position = self._camera_position_block
            other = self._camera_other_block
            return CameraState(target_x=self._read_abs_f32(position + self.CAMERA_TARGET_X_OFFSET), target_y=self._read_abs_f32(position + self.CAMERA_TARGET_Y_OFFSET), distance=self._read_abs_f32(other + self.CAMERA_DISTANCE_OFFSET), yaw=self._read_abs_f32(other + self.CAMERA_YAW_OFFSET), pitch=self._read_abs_f32(other + self.CAMERA_PITCH_OFFSET), roll=self._read_abs_f32(other + self.CAMERA_ROLL_OFFSET), z_offset=self._read_abs_f32(other + self.CAMERA_Z_OFFSET))

    def camera_runtime_session(self) -> CameraRuntimeSession:
        with self._lock:
            if self._camera_position_block is None or self._camera_other_block is None:
                raise SeekBackendError('Камера ещё не подключена')
            position = self._camera_position_block
            other = self._camera_other_block
            return CameraRuntimeSession(process_id=self.process_id, target_x_address=position + self.CAMERA_TARGET_X_OFFSET, target_y_address=position + self.CAMERA_TARGET_Y_OFFSET, distance_address=other + self.CAMERA_DISTANCE_OFFSET, yaw_address=other + self.CAMERA_YAW_OFFSET, pitch_address=other + self.CAMERA_PITCH_OFFSET, roll_address=other + self.CAMERA_ROLL_OFFSET, z_offset_address=other + self.CAMERA_Z_OFFSET, initial_state=self.camera_state())

    def unlock_camera(self, maximum_distance: float=100000.0) -> None:
        if not 10000 <= maximum_distance <= 1000000:
            raise ValueError('Camera limit must be in range 10000..1000000')
        with self._lock:
            if self._camera_other_block is None:
                raise SeekBackendError('Камера ещё не подключена')
            other = self._camera_other_block
            for offset in (self.CAMERA_DISTANCE_MAX_OFFSET, self.CAMERA_VIEW_MAX_OFFSET, self.CAMERA_VIEW_MAX_MIRROR_OFFSET):
                self._write_abs_f32(other + offset, maximum_distance, verify=True)

    def selected_unit(self) -> tuple[int, str]:
        with self._lock:
            if self._selected_unit_pointer is None:
                raise SeekBackendError('Указатель выбранного героя не найден в этой сборке Warcraft.')
            address = self._read_abs_i32(self._selected_unit_pointer)
            if address < 65536:
                raise SeekBackendError('Сначала выбери героя кликом в Warcraft.')
            payload = self._read(address + self.UNIT_RAWCODE_OFFSET, 4)
            raw_value = struct.unpack('<I', payload)[0]
            rawcode = raw_value.to_bytes(4, 'big').decode('latin-1', errors='replace')
            self.unit_camera_position(address)
            return (address, rawcode)

    def unit_camera_position(self, address: int) -> tuple[float, float]:
        with self._lock:
            payload = self._read(address + self.UNIT_X_OFFSET, 8)
            world_x, world_y = struct.unpack('<ff', payload)
            if not (math.isfinite(world_x) and math.isfinite(world_y) and (abs(world_x) <= 100000) and (abs(world_y) <= 100000)):
                raise SeekBackendError('Выбранный объект больше недоступен.')
            return ((world_x + 8192.0) / 32.0, (world_y + 8192.0) / 32.0)

    @classmethod
    def _active_unit_header(cls, payload: bytes, offset: int, expected_vtable: int) -> bool:
        required = offset + cls.UNIT_RUNTIME_ID_OFFSET + 4
        if offset < 0 or required > len(payload):
            return False
        vtable = struct.unpack_from('<I', payload, offset)[0]
        runtime_pointer = struct.unpack_from('<I', payload, offset + cls.UNIT_RUNTIME_POINTER_OFFSET)[0]
        handle_id = struct.unpack_from('<I', payload, offset + cls.UNIT_HANDLE_ID_OFFSET)[0]
        runtime_id = struct.unpack_from('<I', payload, offset + cls.UNIT_RUNTIME_ID_OFFSET)[0]
        invalid_id = 4294967295
        return vtable == expected_vtable and 65536 <= runtime_pointer < cls.MAX_32BIT_ADDRESS and (handle_id != invalid_id) and (runtime_id != invalid_id)

    def _cached_hero_is_active(self, address: int, player_slot: int, raw_value: int, expected_vtable: int) -> bool:
        payload = self._try_read(address, self.UNIT_OWNER_OFFSET + 4)
        if payload is None or not self._active_unit_header(payload, 0, expected_vtable):
            return False
        cached_rawcode = struct.unpack_from('<I', payload, self.UNIT_RAWCODE_OFFSET)[0]
        cached_owner = struct.unpack_from('<I', payload, self.UNIT_OWNER_OFFSET)[0]
        if cached_rawcode != raw_value or cached_owner != player_slot:
            return False
        try:
            self.unit_camera_position(address)
        except SeekBackendError:
            return False
        return True

    def find_player_hero(self, player_slot: int, hero_rawcode: str, cancel: threading.Event | None=None) -> tuple[int, str]:
        if not 0 <= player_slot <= 15:
            raise ValueError('Player slot must be in range 0..15')
        if len(hero_rawcode) != 4:
            raise ValueError('Hero rawcode must contain four characters')
        if self._game_dll_base is None:
            raise SeekBackendError('Game.dll base address is not available')
        raw_value = int.from_bytes(hero_rawcode.encode('latin-1'), 'big')
        expected_vtable = self._game_dll_base + self.UNIT_VTABLE_OFFSET
        cache_key = (player_slot, hero_rawcode)
        with self._hero_cache_lock:
            cached = self._hero_cache.get(cache_key)
        if cached is not None:
            if not self._cached_hero_is_active(cached, player_slot, raw_value, expected_vtable):
                with self._hero_cache_lock:
                    if self._hero_cache.get(cache_key) == cached:
                        self._hero_cache.pop(cache_key, None)
            else:
                return (cached, hero_rawcode)
        signature = struct.pack('<I', raw_value)

        class MEMORY_BASIC_INFORMATION(ctypes.Structure):
            _fields_ = [('BaseAddress', wintypes.LPVOID), ('AllocationBase', wintypes.LPVOID), ('AllocationProtect', wintypes.DWORD), ('PartitionId', wintypes.WORD), ('RegionSize', ctypes.c_size_t), ('State', wintypes.DWORD), ('Protect', wintypes.DWORD), ('Type', wintypes.DWORD)]
        address = 65536
        while address < self.MAX_32BIT_ADDRESS:
            if cancel is not None and cancel.is_set():
                raise SeekCancelled('Hero lookup was cancelled')
            info = MEMORY_BASIC_INFORMATION()
            queried = self._kernel32.VirtualQueryEx(self._handle, ctypes.c_void_p(address), ctypes.byref(info), ctypes.sizeof(info))
            if not queried:
                address += 65536
                continue
            region_base = int(info.BaseAddress or 0)
            region_size = int(info.RegionSize)
            if region_size <= 0:
                address += 65536
                continue
            readable = info.State == self.MEM_COMMIT and (not info.Protect & self.PAGE_GUARD) and (not info.Protect & self.PAGE_NOACCESS)
            if readable:
                chunk_start = region_base
                region_end = min(region_base + region_size, self.MAX_32BIT_ADDRESS)
                overlap = b''
                while chunk_start < region_end:
                    if cancel is not None and cancel.is_set():
                        raise SeekCancelled('Hero lookup was cancelled')
                    chunk_size = min(4 * 1024 * 1024, region_end - chunk_start)
                    chunk = self._try_read(chunk_start, chunk_size)
                    if chunk:
                        haystack = overlap + chunk
                        search_from = 0
                        while True:
                            index = haystack.find(signature, search_from)
                            if index < 0:
                                break
                            candidate_index = index - self.UNIT_RAWCODE_OFFSET
                            if candidate_index >= 0 and candidate_index + self.UNIT_Y_OFFSET + 4 <= len(haystack):
                                owner = struct.unpack_from('<I', haystack, candidate_index + self.UNIT_OWNER_OFFSET)[0]
                                world_x, world_y = struct.unpack_from('<ff', haystack, candidate_index + self.UNIT_X_OFFSET)
                                if self._active_unit_header(haystack, candidate_index, expected_vtable) and owner == player_slot and math.isfinite(world_x) and math.isfinite(world_y) and (abs(world_x) <= 100000) and (abs(world_y) <= 100000):
                                    candidate = chunk_start - len(overlap) + candidate_index
                                    with self._hero_cache_lock:
                                        self._hero_cache[cache_key] = candidate
                                    return (candidate, hero_rawcode)
                            search_from = index + 1
                        keep = self.UNIT_Y_OFFSET + 8
                        overlap = haystack[-keep:]
                    else:
                        overlap = b''
                    chunk_start += chunk_size
            address = region_base + region_size
        raise SeekBackendError('Герой выбранного игрока пока не найден в памяти. Перемотай реплей к моменту после выбора героев.')

    def _validate_replay_block(self, base: int) -> bool:
        if base < 65536 or base > self.MAX_32BIT_ADDRESS:
            return False
        if base & 65535:
            return False
        status = self._try_read(base + self.STATUS_CODE_OFFSET, 4)
        length = self._try_read(base + self.REPLAY_LENGTH_OFFSET, 4)
        position = self._try_read(base + self.REPLAY_POSITION_OFFSET, 4)
        pause = self._try_read(base + self.PAUSE_OFFSET, 4)
        if None in (status, length, position, pause):
            return False
        status_value = struct.unpack('<I', status)[0]
        length_value = struct.unpack('<i', length)[0]
        position_value = struct.unpack('<i', position)[0]
        pause_value = struct.unpack('<i', pause)[0]
        return status_value == self.STATUS_LOOP and 1000 <= length_value <= 12 * 60 * 60 * 1000 and (-1000 <= position_value <= length_value + 60000) and (pause_value in (0, 1))

    def _scan_replay_block(self, progress: Callable[[float], None] | None=None, cancel: threading.Event | None=None) -> int:

        class MEMORY_BASIC_INFORMATION(ctypes.Structure):
            _fields_ = [('BaseAddress', wintypes.LPVOID), ('AllocationBase', wintypes.LPVOID), ('AllocationProtect', wintypes.DWORD), ('PartitionId', wintypes.WORD), ('RegionSize', ctypes.c_size_t), ('State', wintypes.DWORD), ('Protect', wintypes.DWORD), ('Type', wintypes.DWORD)]
        if self._handle is None:
            raise SeekBackendError('Warcraft process is not open')
        address = 65536
        signature = struct.pack('<I', self.STATUS_LOOP)
        while address < self.MAX_32BIT_ADDRESS:
            if cancel is not None and cancel.is_set():
                raise SeekCancelled('Replay scan was cancelled')
            info = MEMORY_BASIC_INFORMATION()
            queried = self._kernel32.VirtualQueryEx(self._handle, ctypes.c_void_p(address), ctypes.byref(info), ctypes.sizeof(info))
            if not queried:
                address += 65536
                continue
            region_base = int(info.BaseAddress or 0)
            region_size = int(info.RegionSize)
            if region_size <= 0:
                address += 65536
                continue
            readable = info.State == self.MEM_COMMIT and (not info.Protect & self.PAGE_GUARD) and (not info.Protect & self.PAGE_NOACCESS)
            if readable:
                chunk_start = region_base
                region_end = min(region_base + region_size, self.MAX_32BIT_ADDRESS)
                overlap = b''
                while chunk_start < region_end:
                    if cancel is not None and cancel.is_set():
                        raise SeekCancelled('Replay scan was cancelled')
                    chunk_size = min(4 * 1024 * 1024, region_end - chunk_start)
                    chunk = self._try_read(chunk_start, chunk_size)
                    if chunk:
                        haystack = overlap + chunk
                        search_from = 0
                        while True:
                            index = haystack.find(signature, search_from)
                            if index < 0:
                                break
                            status_address = chunk_start - len(overlap) + index
                            candidate = status_address - self.STATUS_CODE_OFFSET
                            if self._validate_replay_block(candidate):
                                return candidate
                            search_from = index + 1
                        overlap = haystack[-3:]
                    else:
                        overlap = b''
                    chunk_start += chunk_size
            address = region_base + region_size
            if progress is not None:
                progress(min(address / self.MAX_32BIT_ADDRESS, 1.0))
        raise SeekBackendError('war3.exe is running, but no active 1.26a replay was found')

    def _attach_process_id(self, pid: int) -> ProcessAttachResult:
        process_path = self._query_process_path(pid)
        game_dll, game_match = self._validate_binaries(process_path)
        access = self.PROCESS_QUERY_INFORMATION | self.PROCESS_VM_OPERATION | self.PROCESS_VM_READ | self.PROCESS_VM_WRITE
        handle = self._open_process(pid, access)
        try:
            game_dll_base = self._find_module_base(pid, 'Game.dll')
        except Exception:
            self._kernel32.CloseHandle(handle)
            raise
        self._handle = handle
        self._pid = pid
        self._process_path = process_path
        self._game_dll_match = game_match
        self._game_dll_base = game_dll_base
        return ProcessAttachResult(pid=pid, executable=str(process_path), game_dll=str(game_dll), build_profile=game_match.profile_label, game_dll_match=game_match.match_kind, game_dll_sha256=game_match.sha256)

    def attach_process(self) -> ProcessAttachResult:
        with self._lock:
            self.close()
            processes = self._find_war3_processes()
            if not processes:
                raise SeekBackendError('war3.exe is not running')
            errors: list[str] = []
            for pid, _ in processes:
                try:
                    return self._attach_process_id(pid)
                except SeekBackendError as exc:
                    errors.append(f'PID {pid}: {exc}')
                    self.close()
            raise SeekBackendError('; '.join(errors))

    def attach(self, scan_progress: Callable[[float], None] | None=None, cancel: threading.Event | None=None) -> AttachResult:
        with self._lock:
            self.close()
            processes = self._find_war3_processes()
            if not processes:
                raise SeekBackendError('war3.exe is not running')
            errors: list[str] = []
            for pid, _ in processes:
                try:
                    process_result = self._attach_process_id(pid)
                    self._replay_block = self._scan_replay_block(scan_progress, cancel)
                    return AttachResult(pid=pid, executable=process_result.executable, game_dll=process_result.game_dll, replay_block=self.replay_block, replay_position_ms=self.current_position_ms(), replay_length_ms=self.replay_length_ms(), build_profile=process_result.build_profile, game_dll_match=process_result.game_dll_match, game_dll_sha256=process_result.game_dll_sha256)
                except SeekBackendError as exc:
                    errors.append(f'PID {pid}: {exc}')
                    self.close()
            raise SeekBackendError('; '.join(errors))

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._kernel32.CloseHandle(self._handle)
            self._handle = None
            self._pid = None
            self._process_path = None
            self._game_dll_match = None
            self._game_dll_base = None
            self._replay_block = None
            self._camera_position_block = None
            self._camera_other_block = None
            self._selected_unit_pointer = None
            with self._hero_cache_lock:
                self._hero_cache.clear()

    def current_position_ms(self) -> int:
        with self._lock:
            return self._read_i32(self.REPLAY_POSITION_OFFSET)

    def replay_length_ms(self) -> int:
        with self._lock:
            return self._read_i32(self.REPLAY_LENGTH_OFFSET)

    def is_replay_active(self) -> bool:
        with self._lock:
            return self._read_u32(self.STATUS_CODE_OFFSET) == self.STATUS_LOOP

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self._write_i32(self.PAUSE_OFFSET, 1 if paused else 0)

    def is_paused(self) -> bool:
        with self._lock:
            return self._read_i32(self.PAUSE_OFFSET) == 1

    def set_speed_value(self, value: int, divider: int=1) -> None:
        if value < 1 or value > 65535:
            raise ValueError('Replay speed value must be in range 1..65535')
        if divider < 1 or divider > 65535:
            raise ValueError('Replay speed divider must be in range 1..65535')
        with self._lock:
            self._write_i32(self.REPLAY_SPEED_DIVIDER_OFFSET, divider)
            self._write_i32(self.REPLAY_SPEED_OFFSET, value)

    def _set_temporary_priority(self, priority: int | None) -> int | None:
        if getattr(self, '_pid', None) is None:
            return None
        access = self.PROCESS_QUERY_INFORMATION | self.PROCESS_SET_INFORMATION
        try:
            handle = self._open_process(self._pid, access)
        except SeekBackendError:
            return None
        try:
            previous = int(self._kernel32.GetPriorityClass(handle))
            if priority is not None:
                if not self._kernel32.SetPriorityClass(handle, priority):
                    return None
            return previous or None
        finally:
            self._kernel32.CloseHandle(handle)

    def _restore_priority(self, priority: int | None) -> None:
        if priority is None or getattr(self, '_pid', None) is None:
            return
        access = self.PROCESS_QUERY_INFORMATION | self.PROCESS_SET_INFORMATION
        try:
            handle = self._open_process(self._pid, access)
        except SeekBackendError:
            return
        try:
            self._kernel32.SetPriorityClass(handle, priority)
        finally:
            self._kernel32.CloseHandle(handle)

    def seek_forward(self, target_replay_time_ms: int, cancel: threading.Event, progress: Callable[[SeekProgress], None] | None=None, timeout_seconds: float=20 * 60, profile: SeekProfile | None=None) -> int:
        selected_profile = profile or SEEK_PROFILES['turbo']
        if target_replay_time_ms < 0:
            raise ValueError('Target replay time cannot be negative')
        start_wall_time = time.monotonic()
        last_progress_time = 0.0
        previous_position = self.current_position_ms()
        if target_replay_time_ms + 100 < previous_position:
            raise SeekBackendError('The target is behind the current replay position. Restart/checkpoint support is not connected yet.')
        if target_replay_time_ms - previous_position <= 100:
            self.set_paused(True)
            return previous_position
        speed = selected_profile.maximum_speed
        previous_priority = None
        if selected_profile.lower_process_priority:
            previous_priority = self._set_temporary_priority(self.BELOW_NORMAL_PRIORITY_CLASS)
        try:
            self.set_speed_value(speed)
            self.set_paused(False)
            while True:
                remaining_before_poll = target_replay_time_ms - previous_position
                poll_seconds = 0.04 if remaining_before_poll <= 60000 else selected_profile.far_poll_seconds
                if cancel.wait(poll_seconds):
                    raise SeekCancelled('Seeking was cancelled')
                now = time.monotonic()
                if now - start_wall_time > timeout_seconds:
                    raise SeekBackendError('Seeking timed out')
                if not self.is_replay_active():
                    raise SeekBackendError('Warcraft left replay mode while seeking')
                current = self.current_position_ms()
                remaining = target_replay_time_ms - current
                if progress is not None and (now - last_progress_time >= 0.15 or remaining <= 1500):
                    progress(SeekProgress(current_replay_time_ms=current, target_replay_time_ms=target_replay_time_ms, speed_value=speed))
                    last_progress_time = now
                if remaining <= 100:
                    self.set_paused(True)
                    return current
                delta = max(current - previous_position, 1)
                previous_position = current
                desired_speed = speed
                if remaining <= max(delta * 2, 1500):
                    desired_speed = 2
                elif remaining <= max(delta * 4, 5000):
                    desired_speed = 4
                elif remaining <= max(delta * 8, 15000):
                    desired_speed = 8
                elif remaining <= 60000:
                    desired_speed = min(32, selected_profile.maximum_speed)
                else:
                    desired_speed = selected_profile.maximum_speed
                if desired_speed != speed:
                    speed = desired_speed
                    self.set_speed_value(speed)
        finally:
            try:
                self.set_speed_value(1)
                self.set_paused(True)
            except SeekBackendError:
                pass
            self._restore_priority(previous_priority)

    def __enter__(self) -> Warcraft126MemoryBackend:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
