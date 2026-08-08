from __future__ import annotations
import ctypes
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import BinaryIO
LOGGER_NAME = 'replaylab'
LOG_FILENAME = 'ReplayLab.log'
MAX_LOG_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 3
_CONFIGURE_LOCK = threading.Lock()
_CONFIGURED = False

def diagnostic_log_directory() -> Path:
    local_app_data = os.environ.get('LOCALAPPDATA')
    if local_app_data:
        return Path(local_app_data) / 'ReplayLab' / 'logs'
    return Path.home() / 'AppData' / 'Local' / 'ReplayLab' / 'logs'

def diagnostic_log_path() -> Path:
    return diagnostic_log_directory() / LOG_FILENAME

def _configure_windows_error_mode() -> None:
    if os.name != 'nt':
        return
    try:
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.SetErrorMode.argtypes = [ctypes.c_uint]
        kernel32.SetErrorMode.restype = ctypes.c_uint
        kernel32.SetErrorMode(1 | 2 | 32768)
    except (AttributeError, OSError):
        logging.getLogger(LOGGER_NAME).exception('Could not configure the Windows process error mode')

def configure_diagnostics() -> Path:
    global _CONFIGURED
    with _CONFIGURE_LOCK:
        path = diagnostic_log_path()
        if _CONFIGURED:
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUP_COUNT, encoding='utf-8', delay=True)
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
        root = logging.getLogger(LOGGER_NAME)
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        root.propagate = False
        logging.captureWarnings(True)
        _configure_windows_error_mode()
        previous_hook = sys.excepthook

        def log_unhandled(exception_type: type[BaseException], exception: BaseException, traceback: object) -> None:
            root.critical('Unhandled application exception', exc_info=(exception_type, exception, traceback))
            previous_hook(exception_type, exception, traceback)
        sys.excepthook = log_unhandled
        _CONFIGURED = True
        root.info('ReplayLab diagnostics started; platform=%s; executable=%s', sys.platform, sys.executable)
        return path

def get_logger(component: str) -> logging.Logger:
    return logging.getLogger(f'{LOGGER_NAME}.{component}')

def open_native_stderr(component: str) -> BinaryIO:
    path = configure_diagnostics()
    stream = path.open('ab', buffering=0)
    stream.write(f'\n--- native {component} stderr ---\n'.encode('utf-8'))
    return stream

def is_critical_runtime_error(message: str) -> bool:
    lowered = message.casefold()
    markers = ('переустанови replaylab', 'компонент камеры не найден', 'не найден. камера не запущена', 'не найден. skills hud', 'работает только в windows', 'requires windows', 'protocol mismatch', 'несовместим с программой', 'повреждённый ответ', 'не поддерживается skills hud', 'сборка warcraft не поддерживается', 'unsupported build')
    return any((marker in lowered for marker in markers))

def supported_windows_version(major: int) -> bool:
    return major >= 10
