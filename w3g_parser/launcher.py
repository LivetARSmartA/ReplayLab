from __future__ import annotations
import ctypes
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

class WarcraftLaunchError(RuntimeError):
    pass

@dataclass(frozen=True)
class WarcraftProcess:
    pid: int
    executable: Path | None

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
                    result.append(WarcraftProcess(pid, self._process_path(pid)))
                ok = next_process(snapshot, ctypes.byref(entry))
        finally:
            self._kernel32.CloseHandle(snapshot)
        return result

    def running(self) -> list[WarcraftProcess]:
        return self._running_named({'war3.exe', 'warcraft iii.exe'})

    def running_iccup_launchers(self) -> list[Path]:
        result: list[Path] = []
        for process in self._running_named({'launcher.exe', 'iccup launcher.exe', 'iccupstarlauncher.exe'}):
            path = process.executable
            if path is not None and path.is_file() and ('iccup' in str(path).lower()):
                result.append(path)
        return result

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
                    Desktop, _, send_keys = self._prepare_pywinauto()
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
        if replay_path != staged_replay.resolve():
            shutil.copy2(replay_path, staged_replay)
        command = build_launch_command(game, staged_replay)
        self._owned_process = subprocess.Popen(command, cwd=str(Path(command[0]).parent))
        self._owned_pid = int(self._owned_process.pid)
        self._current_replay = replay_path
        return self._owned_pid

    @staticmethod
    def _prepare_pywinauto() -> tuple[object, object, object]:
        for entry in sys.path:
            dll_directory = Path(entry) / 'pywin32_system32'
            if dll_directory.is_dir() and hasattr(os, 'add_dll_directory'):
                os.add_dll_directory(str(dll_directory.resolve()))
        try:
            from pywinauto import Desktop, mouse
            from pywinauto.keyboard import send_keys
        except (ImportError, OSError) as exc:
            raise WarcraftLaunchError('Не установлен компонент автоматического запуска iCCup.') from exc
        return (Desktop, mouse, send_keys)

    @staticmethod
    def _stage_replay(game: Path, replay_path: Path) -> Path:
        staging_directory = game.parent / 'Replay' / '000_ReplayLab'
        staging_directory.mkdir(parents=True, exist_ok=True)
        staged_replay = staging_directory / 'current.w3g'
        if replay_path != staged_replay.resolve():
            shutil.copy2(replay_path, staged_replay)
        return staged_replay

    def launch_via_iccup(self, iccup_launcher: str | Path, executable: str | Path, replay: str | Path, *, replace_running: bool) -> int:
        build_launch_command(executable, replay)
        launcher_path = Path(iccup_launcher).resolve()
        if not launcher_path.is_file():
            raise WarcraftLaunchError(f'Не найден iCCup Launcher: {launcher_path}')
        game = Path(executable).resolve()
        replay_path = Path(replay).resolve()
        running = self.running()
        if running:
            if not replace_running:
                raise WarcraftLaunchError('Warcraft уже запущен')
            self.close_gracefully(running)
        self._stage_replay(game, replay_path)
        Desktop, mouse, send_keys = self._prepare_pywinauto()
        try:
            launcher_window = None
            launcher_started = False
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                windows = Desktop(backend='uia').windows(title='iCCup Launcher')
                if windows:
                    launcher_window = windows[0]
                    break
                if not launcher_started:
                    subprocess.Popen([str(launcher_path)], cwd=str(launcher_path.parent))
                    launcher_started = True
                time.sleep(0.25)
            if launcher_window is None:
                raise WarcraftLaunchError('iCCup Launcher не открылся за 30 секунд.')
            labels = {'Один игрок', 'Single Player', 'Single player'}
            buttons = [control for control in launcher_window.descendants(control_type='Button') if control.window_text() in labels]
            if not buttons:
                raise WarcraftLaunchError('В iCCup Launcher не найдена кнопка «Один игрок».')
            if not buttons[0].is_enabled():
                raise WarcraftLaunchError('Кнопка «Один игрок» в iCCup Launcher сейчас недоступна.')
            buttons[0].invoke()
            game_window = None
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                windows = [window for window in Desktop(backend='win32').windows() if window.window_text() == 'Warcraft III']
                if windows:
                    game_window = windows[0]
                    break
                time.sleep(0.25)
            if game_window is None:
                raise WarcraftLaunchError('Warcraft не открылся через iCCup за 30 секунд.')
            game_window.restore()
            time.sleep(0.8)
            game_window.set_focus()
            time.sleep(1.0)
            rectangle = game_window.rectangle()
            if rectangle.left < -1000 or rectangle.width() < 640:
                game_window.restore()
                time.sleep(1.0)
                rectangle = game_window.rectangle()
            single_player = (round(rectangle.left + rectangle.width() * 0.852), round(rectangle.top + rectangle.height() * 0.238))
            view_replay = (round(rectangle.left + rectangle.width() * 0.852), round(rectangle.top + rectangle.height() * 0.469))
            mouse.click(coords=single_player)
            time.sleep(0.25)
            mouse.click(coords=single_player)
            time.sleep(2.0)
            mouse.click(coords=view_replay)
            time.sleep(0.25)
            mouse.click(coords=view_replay)
            time.sleep(3.0)
            send_keys('{HOME}{ENTER}', pause=0.15)
            time.sleep(1.0)
            send_keys('{HOME}{DOWN}{ENTER}', pause=0.15)
        except WarcraftLaunchError:
            raise
        except Exception as exc:
            raise WarcraftLaunchError('Не удалось автоматически открыть реплей через iCCup Launcher.') from exc
        self._owned_process = None
        self._owned_pid = int(game_window.process_id())
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
