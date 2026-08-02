from __future__ import annotations
import ctypes
import hashlib
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from .warcraft_build import WARCRAFT_126A_WAR3_SHA256, WarcraftBuildError, match_game_dll
from .warcraft_glue import MapListState, WarcraftGlueChannel, WarcraftGlueError

class WarcraftLaunchError(RuntimeError):
    pass

@dataclass(frozen=True)
class WarcraftProcess:
    pid: int
    executable: Path | None
    parent_pid: int | None = None

@dataclass(frozen=True)
class WarcraftWindow:
    pid: int
    handle: int

class _WindowInputGuard:

    def __init__(self, user32: object) -> None:
        self._user32 = user32
        self._windows: list[tuple[int, bool]] = []

    def __enter__(self) -> _WindowInputGuard:
        return self

    def __exit__(self, exception_type: object, _exception: object, _traceback: object) -> None:
        restore_failed = False
        for handle, was_enabled in reversed(self._windows):
            if was_enabled and self._user32.IsWindow(handle):
                self._user32.EnableWindow(handle, True)
                restore_failed = restore_failed or not self._user32.IsWindowEnabled(handle)
        if restore_failed and exception_type is None:
            raise WarcraftLaunchError('Replay запущен, но Windows не смогла вернуть ввод одному из временно заблокированных окон.')

    def lock(self, handle: int) -> None:
        if any((known == handle for known, _ in self._windows)):
            return
        was_enabled = bool(self._user32.IsWindowEnabled(handle))
        self._windows.append((handle, was_enabled))
        if not was_enabled:
            return
        self._user32.EnableWindow(handle, False)
        if self._user32.IsWindowEnabled(handle):
            raise WarcraftLaunchError('Windows не разрешила временно защитить окно от пользовательского ввода.')
_PYWIN32_DLL_DIRECTORY_HANDLES: list[object] = []
_PYWIN32_DLL_DIRECTORIES: set[str] = set()

def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest().upper()

def build_launch_command(executable: str | Path, replay: str | Path) -> list[str]:
    game = Path(executable).resolve()
    replay_path = Path(replay).resolve()
    if not game.is_file():
        raise WarcraftLaunchError(f'Не найден Warcraft: {game}')
    if not replay_path.is_file():
        raise WarcraftLaunchError(f'Не найден реплей: {replay_path}')
    if replay_path.suffix.lower() != '.w3g':
        raise WarcraftLaunchError('Warcraft можно запустить только с .w3g')
    return [str(game), '-loadfile', str(replay_path)]

class WarcraftReplayLauncher:
    TH32CS_SNAPPROCESS = 2
    PROCESS_QUERY_LIMITED_INFORMATION = 4096
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    WM_CLOSE = 16
    SW_RESTORE = 9
    VK_S = (83, 31, False)
    VK_R = (82, 19, False)
    VK_HOME = (36, 71, True)
    VK_DOWN = (40, 80, True)
    VK_ENTER = (13, 28, False)

    def __init__(self) -> None:
        if os.name != 'nt':
            raise WarcraftLaunchError('Запуск Warcraft поддерживается только в Windows')
        self._kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self._user32 = ctypes.WinDLL('user32', use_last_error=True)
        self._configure_winapi()
        self._owned_process: subprocess.Popen[bytes] | None = None
        self._owned_pid: int | None = None
        self._current_replay: Path | None = None

    def owns_process(self, pid: int) -> bool:
        if self._owned_pid != pid:
            return False
        return any((process.pid == pid for process in self.running()))

    def is_current_replay(self, replay: str | Path) -> bool:
        return self._owned_pid is not None and any((process.pid == self._owned_pid for process in self.running())) and (self._current_replay == Path(replay).resolve())

    def _configure_winapi(self) -> None:
        self._kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self._kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self._user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user32.IsWindow.argtypes = [wintypes.HWND]
        self._user32.IsWindow.restype = wintypes.BOOL
        self._user32.IsWindowEnabled.argtypes = [wintypes.HWND]
        self._user32.IsWindowEnabled.restype = wintypes.BOOL
        self._user32.EnableWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
        self._user32.EnableWindow.restype = wintypes.BOOL
        self._user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self._user32.ShowWindow.restype = wintypes.BOOL
        self._user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self._user32.PostMessageW.restype = wintypes.BOOL

    def _process_path(self, pid: int) -> Path | None:
        handle = self._kernel32.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not self._kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return None
            return Path(buffer.value)
        finally:
            self._kernel32.CloseHandle(handle)

    def _running_named(self, executable_names: set[str]) -> list[WarcraftProcess]:

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD), ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG), ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]
        create_snapshot = self._kernel32.CreateToolhelp32Snapshot
        create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        create_snapshot.restype = wintypes.HANDLE
        first = self._kernel32.Process32FirstW
        first.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        first.restype = wintypes.BOOL
        next_process = self._kernel32.Process32NextW
        next_process.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        next_process.restype = wintypes.BOOL
        snapshot = create_snapshot(self.TH32CS_SNAPPROCESS, 0)
        if int(snapshot) == self.INVALID_HANDLE_VALUE:
            raise WarcraftLaunchError('Не удалось получить список процессов Windows')
        result: list[WarcraftProcess] = []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            ok = first(snapshot, ctypes.byref(entry))
            while ok:
                if entry.szExeFile.lower() in executable_names:
                    pid = int(entry.th32ProcessID)
                    result.append(WarcraftProcess(pid, self._process_path(pid), int(entry.th32ParentProcessID)))
                ok = next_process(snapshot, ctypes.byref(entry))
        finally:
            self._kernel32.CloseHandle(snapshot)
        return result

    def running(self) -> list[WarcraftProcess]:
        return self._running_named({'war3.exe', 'warcraft iii.exe'})

    def _running_iccup_processes(self) -> list[WarcraftProcess]:
        return [process for process in self._running_named({'launcher.exe', 'iccup launcher.exe', 'iccupstarlauncher.exe'}) if process.executable is not None and process.executable.is_file() and ('iccup' in str(process.executable).lower())]

    def running_iccup_launchers(self) -> list[Path]:
        return [process.executable for process in self._running_iccup_processes() if process.executable is not None]

    def _find_window(self, pid: int, *, title: str | None=None) -> WarcraftWindow | None:
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        found: list[int] = []

        def inspect(window: int, _: int) -> bool:
            window_pid = wintypes.DWORD()
            self._user32.GetWindowThreadProcessId(window, ctypes.byref(window_pid))
            if int(window_pid.value) != pid:
                return True
            length = self._user32.GetWindowTextLengthW(window)
            buffer = ctypes.create_unicode_buffer(length + 1)
            self._user32.GetWindowTextW(window, buffer, len(buffer))
            if title is not None and buffer.value != title:
                return True
            found.append(int(window))
            return False
        callback = callback_type(inspect)
        self._user32.EnumWindows(callback, 0)
        if not found:
            return None
        return WarcraftWindow(pid=pid, handle=found[0])

    def close_gracefully(self, processes: list[WarcraftProcess], timeout_seconds: float=8.0) -> None:
        pids = {process.pid for process in processes}
        if not pids:
            return
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def close_window(window: int, _: int) -> bool:
            pid = wintypes.DWORD()
            self._user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
            if int(pid.value) in pids:
                self._user32.PostMessageW(window, self.WM_CLOSE, 0, 0)
            return True
        callback = callback_type(close_window)
        self._user32.EnumWindows(callback, 0)
        deadline = time.monotonic() + timeout_seconds
        alt_f4_sent = False
        while time.monotonic() < deadline:
            alive = {process.pid for process in self.running()}
            if not alive & pids:
                return
            if not alt_f4_sent and time.monotonic() >= deadline - timeout_seconds / 2:
                try:
                    Desktop = self._prepare_pywinauto()
                    from pywinauto.keyboard import send_keys
                    windows = [window for window in Desktop(backend='win32').windows() if int(window.process_id()) in alive & pids]
                    if windows:
                        windows[0].restore()
                        windows[0].set_focus()
                        send_keys('%{F4}')
                except Exception:
                    pass
                alt_f4_sent = True
            time.sleep(0.1)
        raise WarcraftLaunchError('Warcraft не закрылся сам. Закрой игру вручную и повтори запуск.')

    def launch(self, executable: str | Path, replay: str | Path, *, replace_running: bool) -> int:
        build_launch_command(executable, replay)
        game = Path(executable).resolve()
        replay_path = Path(replay).resolve()
        running = self.running()
        if running:
            if not replace_running:
                raise WarcraftLaunchError('Warcraft уже запущен')
            self.close_gracefully(running)
        staging_directory = game.parent / 'Replay' / 'ReplayLab'
        staging_directory.mkdir(parents=True, exist_ok=True)
        staged_replay = staging_directory / 'current.w3g'
        self._copy_replay_atomically(replay_path, staged_replay)
        command = build_launch_command(game, staged_replay)
        self._owned_process = subprocess.Popen(command, cwd=str(Path(command[0]).parent))
        self._owned_pid = int(self._owned_process.pid)
        self._current_replay = replay_path
        return self._owned_pid

    @staticmethod
    def _prepare_pywinauto() -> object:
        for entry in sys.path:
            dll_directory = Path(entry) / 'pywin32_system32'
            if dll_directory.is_dir() and hasattr(os, 'add_dll_directory'):
                resolved = str(dll_directory.resolve())
                if resolved not in _PYWIN32_DLL_DIRECTORIES:
                    _PYWIN32_DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(resolved))
                    _PYWIN32_DLL_DIRECTORIES.add(resolved)
        try:
            from pywinauto import Desktop
        except (ImportError, OSError) as exc:
            raise WarcraftLaunchError('Не установлен компонент автоматического запуска iCCup.') from exc
        return Desktop

    @staticmethod
    def _copy_replay_atomically(replay_path: Path, staged_replay: Path) -> None:
        source_hash = _file_sha256(replay_path)
        if replay_path != staged_replay.resolve():
            pending = staged_replay.with_name(staged_replay.name + '.pending')
            try:
                shutil.copy2(replay_path, pending)
                if _file_sha256(pending) != source_hash:
                    raise WarcraftLaunchError('Проверка staged replay не прошла: временная копия отличается от выбранного файла.')
                os.replace(pending, staged_replay)
            finally:
                if pending.is_file():
                    pending.unlink()
        if _file_sha256(staged_replay) != source_hash:
            raise WarcraftLaunchError('Проверка staged replay не прошла: итоговая копия отличается от выбранного файла.')

    @staticmethod
    def _stage_replay(game: Path, replay_path: Path) -> Path:
        staging_directory = game.parent / 'Replay' / '000_ReplayLab'
        staging_directory.mkdir(parents=True, exist_ok=True)
        managed_names = {'current.w3g', 'current.w3g.pending'}
        unexpected = [entry.name for entry in staging_directory.iterdir() if entry.name.casefold() not in managed_names or (entry.name.casefold() == 'current.w3g.pending' and (not entry.is_file()))]
        if unexpected:
            names = ', '.join(sorted(unexpected))
            raise WarcraftLaunchError(f'Служебная папка ReplayLab содержит посторонние файлы: {names}. Убери их из {staging_directory}.')
        staged_replay = staging_directory / 'current.w3g'
        WarcraftReplayLauncher._copy_replay_atomically(replay_path, staged_replay)
        return staged_replay

    @staticmethod
    def _post_key(channel: WarcraftGlueChannel, key: tuple[int, int, bool]) -> None:
        virtual_key, scan_code, extended = key
        channel.post_key(virtual_key, scan_code, extended=extended)

    def _wait_for_signature(self, channel: WarcraftGlueChannel, target: bytes, *, phase: str, timeout_seconds: float, source: bytes | None=None, transition_key: tuple[int, int, bool] | None=None) -> None:
        deadline = time.monotonic() + timeout_seconds
        next_attempt = 0.0
        source_seen = source is None
        patterns = {target}
        if source is not None:
            patterns.add(source)
        while time.monotonic() < deadline:
            found = channel.private_signatures(patterns)
            if target in found:
                return
            now = time.monotonic()
            if source is not None and source in found:
                source_seen = True
                if transition_key is not None and now >= next_attempt:
                    self._post_key(channel, transition_key)
                    next_attempt = now + 1.0
            time.sleep(0.05)
        detail = 'исходный экран подтверждён' if source_seen else 'исходный экран не появился'
        raise WarcraftLaunchError(f'Таймаут этапа «{phase}»: {detail}, но целевой экран Warcraft не подтверждён.')

    @staticmethod
    def _wait_for_map_list(channel: WarcraftGlueChannel, predicate: Callable[[MapListState], bool], *, phase: str, timeout_seconds: float, action: Callable[[], None] | None=None) -> MapListState:
        deadline = time.monotonic() + timeout_seconds
        next_attempt = 0.0
        last_state: MapListState | None = None
        while time.monotonic() < deadline:
            state = channel.map_list_state()
            last_state = state
            if state is not None and predicate(state):
                return state
            now = time.monotonic()
            if state is not None and action is not None and (now >= next_attempt):
                action()
                next_attempt = now + 1.0
            time.sleep(0.05)
        state_note = 'CMapList не найден' if last_state is None else f'каталог={last_state.current_directory!r}, индекс={last_state.selected_index}'
        raise WarcraftLaunchError(f'Таймаут этапа «{phase}»: {state_note}.')

    def _open_staged_replay(self, channel: WarcraftGlueChannel, staging_directory: Path) -> None:
        self._wait_for_signature(channel, WarcraftGlueChannel.CMAIN_MENU, phase='главное меню', timeout_seconds=30.0)
        self._wait_for_signature(channel, WarcraftGlueChannel.CSINGLE_PLAYER_MENU, source=WarcraftGlueChannel.CMAIN_MENU, transition_key=self.VK_S, phase='Один игрок', timeout_seconds=15.0)
        self._wait_for_signature(channel, WarcraftGlueChannel.CVIEW_REPLAY_SCREEN, source=WarcraftGlueChannel.CSINGLE_PLAYER_MENU, transition_key=self.VK_R, phase='Загрузка ролика', timeout_seconds=15.0)
        self._wait_for_signature(channel, WarcraftGlueChannel.CMAP_LIST, phase='список replay', timeout_seconds=10.0)
        self._wait_for_map_list(channel, lambda state: state.current_directory == '', phase='корень Replay', timeout_seconds=10.0)
        expected_directory = staging_directory.name + '\\'

        def enter_staging_directory() -> None:
            self._post_key(channel, self.VK_HOME)
            self._post_key(channel, self.VK_ENTER)
        self._wait_for_map_list(channel, lambda state: state.current_directory.casefold() == expected_directory.casefold(), phase=f'каталог {staging_directory.name}', timeout_seconds=15.0, action=enter_staging_directory)

        def select_current() -> None:
            self._post_key(channel, self.VK_HOME)
            self._post_key(channel, self.VK_DOWN)
        selected = self._wait_for_map_list(channel, lambda state: state.selected_index == 1 and state.selected_pointer >= 65536, phase='выбор current.w3g', timeout_seconds=10.0, action=select_current)
        if selected.current_directory.casefold() != expected_directory.casefold():
            raise WarcraftLaunchError('Warcraft выбрал current.w3g не в служебном каталоге ReplayLab.')

    @staticmethod
    def _guard_log_candidates(launcher_path: Path, local_app_data: Path | None=None) -> tuple[Path, ...]:
        launcher_directory = launcher_path.resolve().parent
        candidates = [launcher_directory / '__log.txt']
        if local_app_data is None:
            local_app_data_value = os.environ.get('LOCALAPPDATA', '').strip()
            if local_app_data_value:
                local_app_data = Path(local_app_data_value)
        if local_app_data is not None and launcher_directory.anchor:
            try:
                relative_directory = launcher_directory.relative_to(Path(launcher_directory.anchor))
            except ValueError:
                pass
            else:
                candidates.append(local_app_data / 'VirtualStore' / relative_directory / '__log.txt')
        unique_candidates: list[Path] = []
        known: set[str] = set()
        for candidate in candidates:
            key = str(candidate).casefold()
            if key in known:
                continue
            known.add(key)
            unique_candidates.append(candidate)
        return tuple(unique_candidates)

    @staticmethod
    def _guard_log_snapshot(candidates: tuple[Path, ...]) -> tuple[dict[Path, tuple[int, int]], dict[Path, OSError]]:
        snapshot: dict[Path, tuple[int, int]] = {}
        errors: dict[Path, OSError] = {}
        for candidate in candidates:
            try:
                with candidate.open('rb') as source:
                    source.seek(0, os.SEEK_END)
                    size = source.tell()
                modified_ns = candidate.stat().st_mtime_ns
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError as exc:
                errors[candidate] = exc
                continue
            snapshot[candidate] = (size, modified_ns)
        return (snapshot, errors)

    @classmethod
    def _wait_for_guard_log(cls, candidates: tuple[Path, ...], baseline: dict[Path, tuple[int, int]], timeout_seconds: float=10.0) -> Path:
        deadline = time.monotonic() + timeout_seconds
        latest_snapshot: dict[Path, tuple[int, int]] = {}
        latest_errors: dict[Path, OSError] = {}
        while True:
            latest_snapshot, latest_errors = cls._guard_log_snapshot(candidates)
            changed = [candidate for candidate, state in latest_snapshot.items() if baseline.get(candidate) != state]
            if changed:
                return max(changed, key=lambda candidate: latest_snapshot[candidate][1])
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        if latest_snapshot:
            return max(latest_snapshot, key=lambda candidate: latest_snapshot[candidate][1])
        if latest_errors:
            candidate, error = next(iter(latest_errors.items()))
            raise WarcraftLaunchError(f'Диагностический журнал iCCup найден, но ReplayLab не может его прочитать: {candidate} ({error})')
        checked = '\n'.join((f'- {candidate}' for candidate in candidates))
        raise WarcraftLaunchError(f'iCCup не создал диагностический журнал после запуска Warcraft. Проверены пути:\n{checked}')

    @staticmethod
    def _read_guard_delta(guard_log: Path, start_offset: int) -> str:
        if not guard_log.is_file():
            raise WarcraftLaunchError(f'Не найден диагностический журнал iCCup: {guard_log}')
        if guard_log.stat().st_size < start_offset:
            raise WarcraftLaunchError('Журнал iCCup был очищен во время запуска replay.')
        with guard_log.open('rb') as source:
            source.seek(start_offset)
            return source.read().decode('utf-8', errors='replace')

    def _wait_for_guard_confirmation(self, guard_log: Path, start_offset: int, timeout_seconds: float=20.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_text = ''
        while time.monotonic() < deadline:
            last_text = self._read_guard_delta(guard_log, start_offset)
            if 'Loading ICCup replay file' in last_text and 'Got MapFileInfo' in last_text:
                return
            time.sleep(0.1)
        loaded = 'Loading ICCup replay file' in last_text
        raise WarcraftLaunchError('Таймаут подтверждения iCCup: ' + ('replay принят, но MapFileInfo не появился.' if loaded else 'guard не подтвердил загрузку staged replay.'))

    @staticmethod
    def _wait_for_live_replay(pid: int, timeout_seconds: float=30.0) -> None:
        from .seeker import SeekBackendError, Warcraft126MemoryBackend
        deadline = time.monotonic() + timeout_seconds
        last_error = 'active replay block не найден'
        while time.monotonic() < deadline:
            backend = Warcraft126MemoryBackend()
            try:
                result = backend.attach()
                if result.pid != pid:
                    last_error = f'replay найден в PID {result.pid}, ожидался PID {pid}'
                elif backend.is_replay_active():
                    return
                else:
                    last_error = 'replay block найден, но LOOP не активен'
            except SeekBackendError as exc:
                last_error = str(exc)
            finally:
                backend.close()
            time.sleep(0.1)
        raise WarcraftLaunchError('Таймаут этапа «живой replay»: ' + last_error)

    def launch_via_iccup(self, iccup_launcher: str | Path, executable: str | Path, replay: str | Path, *, replace_running: bool) -> int:
        build_launch_command(executable, replay)
        launcher_path = Path(iccup_launcher).resolve()
        if not launcher_path.is_file():
            raise WarcraftLaunchError(f'Не найден iCCup Launcher: {launcher_path}')
        game = Path(executable).resolve()
        replay_path = Path(replay).resolve()
        if _file_sha256(game) != WARCRAFT_126A_WAR3_SHA256:
            raise WarcraftLaunchError('Cursor-free запуск поддерживает только проверенный war3.exe Warcraft III 1.26a build 6401.')
        try:
            game_dll_match = match_game_dll(game.with_name('Game.dll'))
        except (OSError, WarcraftBuildError) as exc:
            raise WarcraftLaunchError(f'Cursor-free запуск отключён: Game.dll не прошёл проверку Warcraft III 1.26a ({exc}).') from exc
        if not game_dll_match.exact:
            raise WarcraftLaunchError('Cursor-free запуск отключён: найден совместимый, но не точный Game.dll Warcraft III 1.26a. Смещения replay-браузера для этой сборки ещё не верифицированы.')
        running = self.running()
        if running:
            if not replace_running:
                raise WarcraftLaunchError('Warcraft уже запущен')
            self.close_gracefully(running)
        staged_replay = self._stage_replay(game, replay_path)
        staging_directory = staged_replay.parent
        Desktop = self._prepare_pywinauto()
        try:
            launcher_window = None
            launcher_started = False
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                launcher_processes = self._running_iccup_processes()
                for process in launcher_processes:
                    window = self._find_window(process.pid, title='iCCup Launcher')
                    if window is not None:
                        self._user32.ShowWindow(window.handle, self.SW_RESTORE)
                windows = Desktop(backend='uia').windows(title='iCCup Launcher')
                matching = []
                for window in windows:
                    path = self._process_path(int(window.process_id()))
                    if path is not None and path.resolve() == launcher_path:
                        matching.append(window)
                if matching:
                    launcher_window = matching[0]
                    break
                if not launcher_started:
                    subprocess.Popen([str(launcher_path)], cwd=str(launcher_path.parent))
                    launcher_started = True
                time.sleep(0.1)
            if launcher_window is None:
                raise WarcraftLaunchError('iCCup Launcher не открылся за 30 секунд.')
            launcher_pid = int(launcher_window.process_id())
            labels = {'Один игрок', 'Single Player', 'Single player'}
            buttons = [control for control in launcher_window.descendants(control_type='Button') if control.window_text() in labels]
            if not buttons:
                raise WarcraftLaunchError('В iCCup Launcher не найдена кнопка «Один игрок».')
            if not buttons[0].is_enabled():
                raise WarcraftLaunchError('Кнопка «Один игрок» в iCCup Launcher сейчас недоступна.')
            guard_candidates = self._guard_log_candidates(launcher_path)
            guard_baseline, _ = self._guard_log_snapshot(guard_candidates)
            previous_pids = {process.pid for process in self.running()}
            buttons[0].invoke()
            with _WindowInputGuard(self._user32) as input_guard:
                launcher_native_window = self._find_window(launcher_pid, title='iCCup Launcher')
                if launcher_native_window is not None:
                    input_guard.lock(launcher_native_window.handle)
                game_process = None
                deadline = time.monotonic() + 30.0
                while time.monotonic() < deadline:
                    candidates = [process for process in self.running() if process.pid not in previous_pids and process.executable is not None and (process.executable.resolve() == game) and (process.parent_pid == launcher_pid)]
                    if candidates:
                        game_process = candidates[0]
                        break
                    time.sleep(0.05)
                if game_process is None:
                    raise WarcraftLaunchError('iCCup не создал проверенный дочерний процесс war3.exe за 30 секунд.')
                game_window = None
                deadline = time.monotonic() + 30.0
                while time.monotonic() < deadline:
                    game_window = self._find_window(game_process.pid, title='Warcraft III')
                    if game_window is not None:
                        input_guard.lock(game_window.handle)
                        break
                    time.sleep(0.05)
                if game_window is None:
                    raise WarcraftLaunchError('Процесс Warcraft создан iCCup, но окно «Warcraft III» не появилось за 30 секунд.')
                guard_log = self._wait_for_guard_log(guard_candidates, guard_baseline)
                self._user32.ShowWindow(game_window.handle, self.SW_RESTORE)
                with WarcraftGlueChannel(self._kernel32, self._user32, game_process.pid, game_window.handle, game) as channel:
                    self._open_staged_replay(channel, staging_directory)
                    guard_offset = guard_log.stat().st_size
                    self._post_key(channel, self.VK_ENTER)
                    self._wait_for_guard_confirmation(guard_log, guard_offset)
                    self._wait_for_live_replay(game_process.pid)
        except WarcraftLaunchError:
            raise
        except WarcraftGlueError as exc:
            raise WarcraftLaunchError(str(exc)) from exc
        except Exception as exc:
            raise WarcraftLaunchError(f'Cursor-free запуск через iCCup завершился ошибкой: {type(exc).__name__}: {exc}') from exc
        self._owned_process = None
        self._owned_pid = game_process.pid
        self._current_replay = replay_path
        return self._owned_pid

def likely_warcraft_executables(configured: str | Path | None=None) -> list[Path]:
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    candidates.extend([Path('D:\\Warcraft 3\\war3.exe'), Path('C:\\Warcraft III\\war3.exe'), Path('C:\\Program Files (x86)\\Warcraft III\\war3.exe')])
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique

def likely_iccup_launchers(configured: str | Path | None=None) -> list[Path]:
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    candidates.extend([Path('D:\\ICCupGameLauncher\\Launcher\\Launcher.exe'), Path('C:\\ICCupGameLauncher\\Launcher\\Launcher.exe'), Path('C:\\Program Files (x86)\\ICCupGameLauncher\\Launcher\\Launcher.exe'), Path('C:\\Program Files\\ICCupGameLauncher\\Launcher\\Launcher.exe')])
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique
