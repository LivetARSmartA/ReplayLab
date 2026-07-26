from __future__ import annotations
import ctypes
import os
import threading
from ctypes import wintypes
from typing import Callable
from .seeker import SeekBackendError
CAMERA_CONTROL_KEYS = frozenset({33, 34, 35, 36, 37, 38, 39, 40, 45, 46, 96, 97})
KEY_CHOICES = tuple(((f'F{number}', 111 + number) for number in range(1, 13))) + tuple(((str(number), 48 + number) for number in range(10))) + tuple(((letter, ord(letter)) for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ')) + tuple(((f'Num {number}', 96 + number) for number in range(10))) + (('Num *', 106), ('Num +', 107), ('Num -', 109), ('Num .', 110), ('Num /', 111), ('Space', 32), ('Tab', 9), ('Enter', 13), ('Backspace', 8), ('Esc', 27), ('Insert', 45), ('Delete', 46), ('Home', 36), ('End', 35), ('Page Up', 33), ('Page Down', 34), ('←', 37), ('↑', 38), ('→', 39), ('↓', 40), ('Left Shift', 160), ('Right Shift', 161), ('Left Ctrl', 162), ('Right Ctrl', 163), ('Left Alt', 164), ('Right Alt', 165), (';', 186), ('=', 187), (',', 188), ('-', 189), ('.', 190), ('/', 191), ('`', 192), ('[', 219), ('\\', 220), (']', 221), ("'", 222))

class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('vkCode', wintypes.DWORD), ('scanCode', wintypes.DWORD), ('flags', wintypes.DWORD), ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t)]

class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [('pt', wintypes.POINT), ('mouseData', wintypes.DWORD), ('flags', wintypes.DWORD), ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t)]

class CameraInputRouter:
    WH_KEYBOARD_LL = 13
    WH_MOUSE_LL = 14
    WM_KEYDOWN = 256
    WM_KEYUP = 257
    WM_SYSKEYDOWN = 260
    WM_SYSKEYUP = 261
    WM_MOUSEMOVE = 512
    WM_QUIT = 18

    def __init__(self, on_action: Callable[[str], None]) -> None:
        if os.name != 'nt':
            raise SeekBackendError('Camera input router requires Windows')
        self._on_action = on_action
        self._bindings: dict[int, str] = {}
        self._camera_process_id: int | None = None
        self._pressed: set[int] = set()
        self._macro_down: set[int] = set()
        self._follow_active = False
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._startup_error: str | None = None
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._hook: int | None = None
        self._callback: object | None = None
        self._mouse_hook: int | None = None
        self._mouse_callback: object | None = None
        self._user32 = ctypes.WinDLL('user32', use_last_error=True)
        self._kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self._hook_proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        self._user32.GetWindowTextLengthW.restype = ctypes.c_int
        self._user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self._user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self._user32.GetClientRect.restype = wintypes.BOOL
        self._user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        self._user32.ClientToScreen.restype = wintypes.BOOL
        self._user32.SetWindowsHookExW.argtypes = [ctypes.c_int, self._hook_proc_type, wintypes.HINSTANCE, wintypes.DWORD]
        self._user32.SetWindowsHookExW.restype = wintypes.HHOOK
        self._user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        self._user32.CallNextHookEx.restype = ctypes.c_ssize_t
        self._user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        self._user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        self._user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        self._user32.GetMessageW.restype = wintypes.BOOL
        self._user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self._user32.PostThreadMessageW.restype = wintypes.BOOL
        self._kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self._kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self._kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    def set_bindings(self, bindings: dict[str, int]) -> None:
        keys = list(bindings.values())
        if len(keys) != len(set(keys)):
            raise ValueError('Одна клавиша не может запускать два макроса')
        with self._lock:
            self._bindings = {int(key): str(action) for action, key in bindings.items()}
            self._pressed.clear()
            self._macro_down.clear()

    def set_camera_process(self, process_id: int | None) -> None:
        with self._lock:
            self._camera_process_id = process_id
            self._pressed.clear()
            self._macro_down.clear()
            if process_id is None:
                self._follow_active = False

    def set_follow_active(self, active: bool) -> None:
        with self._lock:
            self._follow_active = bool(active) and self._camera_process_id is not None

    def is_pressed(self, virtual_key: int) -> bool:
        with self._lock:
            return virtual_key in self._pressed

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(target=self._run, name='replaylab-camera-input', daemon=True)
        self._thread.start()
        if not self._ready.wait(2.0):
            raise SeekBackendError('Camera Macro Engine не ответил при запуске')
        if self._startup_error is not None:
            raise SeekBackendError(self._startup_error)

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        if self._thread_id:
            self._user32.PostThreadMessageW(self._thread_id, self.WM_QUIT, 0, 0)
        if thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None
        self._thread_id = 0
        with self._lock:
            self._pressed.clear()
            self._macro_down.clear()
            self._camera_process_id = None
            self._follow_active = False

    def _foreground_context(self) -> tuple[int, bool, int]:
        window = self._user32.GetForegroundWindow()
        if not window:
            return (0, False, 0)
        pid = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
        with self._lock:
            camera_pid = self._camera_process_id
        if camera_pid is not None:
            return (int(window), int(pid.value) == camera_pid, int(pid.value))
        length = self._user32.GetWindowTextLengthW(window)
        title = ctypes.create_unicode_buffer(max(length + 1, 2))
        self._user32.GetWindowTextW(window, title, len(title))
        return (int(window), title.value == 'Warcraft III', int(pid.value))

    def _hook_callback(self, code: int, message: int, data: int) -> int:
        if code >= 0:
            event = ctypes.cast(data, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            key = int(event.vkCode)
            _, focused, _ = self._foreground_context()
            with self._lock:
                action = self._bindings.get(key)
                camera_active = self._camera_process_id is not None
            intercepted = action is not None or (camera_active and key in CAMERA_CONTROL_KEYS)
            if intercepted:
                if message in (self.WM_KEYDOWN, self.WM_SYSKEYDOWN):
                    first_press = False
                    if focused:
                        with self._lock:
                            if action is not None:
                                first_press = key not in self._macro_down
                                self._macro_down.add(key)
                            else:
                                first_press = key not in self._pressed
                                self._pressed.add(key)
                    if focused and first_press and (action is not None):
                        self._on_action(action)
                elif message in (self.WM_KEYUP, self.WM_SYSKEYUP):
                    with self._lock:
                        self._pressed.discard(key)
                        self._macro_down.discard(key)
                if focused:
                    return 1
            elif not focused:
                with self._lock:
                    self._pressed.clear()
                    self._macro_down.clear()
        return int(self._user32.CallNextHookEx(self._hook or 0, code, message, data))

    def _mouse_hook_callback(self, code: int, message: int, data: int) -> int:
        if code >= 0 and message == self.WM_MOUSEMOVE:
            with self._lock:
                follow_active = self._follow_active
            if follow_active:
                window, focused, _ = self._foreground_context()
                if focused and window:
                    event = ctypes.cast(data, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    client = wintypes.RECT()
                    top_left = wintypes.POINT()
                    bottom_right = wintypes.POINT()
                    if self._user32.GetClientRect(window, ctypes.byref(client)) and self._user32.ClientToScreen(window, ctypes.byref(top_left)):
                        bottom_right.x = client.right
                        bottom_right.y = client.bottom
                        if self._user32.ClientToScreen(window, ctypes.byref(bottom_right)):
                            edge = 20
                            if event.pt.x <= top_left.x + edge or event.pt.x >= bottom_right.x - edge or event.pt.y <= top_left.y + edge or (event.pt.y >= bottom_right.y - edge):
                                return 1
        return int(self._user32.CallNextHookEx(self._mouse_hook or 0, code, message, data))

    def _run(self) -> None:
        self._thread_id = int(self._kernel32.GetCurrentThreadId())
        callback = self._hook_proc_type(self._hook_callback)
        module = self._kernel32.GetModuleHandleW(None)
        hook = self._user32.SetWindowsHookExW(self.WH_KEYBOARD_LL, callback, module, 0)
        if not hook:
            error = ctypes.get_last_error()
            self._startup_error = f'Не удалось запустить Camera Macro Engine (WinError {error})'
            self._ready.set()
            return
        self._callback = callback
        self._hook = int(hook)
        mouse_callback = self._hook_proc_type(self._mouse_hook_callback)
        mouse_hook = self._user32.SetWindowsHookExW(self.WH_MOUSE_LL, mouse_callback, module, 0)
        if not mouse_hook:
            error = ctypes.get_last_error()
            self._user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
            self._callback = None
            self._startup_error = f'Не удалось запустить защиту Follow от edge-scroll (WinError {error})'
            self._ready.set()
            return
        self._mouse_callback = mouse_callback
        self._mouse_hook = int(mouse_hook)
        self._ready.set()
        message = wintypes.MSG()
        while self._user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            pass
        self._user32.UnhookWindowsHookEx(self._hook)
        self._user32.UnhookWindowsHookEx(self._mouse_hook)
        self._hook = None
        self._callback = None
        self._mouse_hook = None
        self._mouse_callback = None
