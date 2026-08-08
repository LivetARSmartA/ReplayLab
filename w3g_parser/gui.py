from __future__ import annotations
import concurrent.futures
import math
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable
from PySide6.QtCore import QAbstractAnimation, QEasingCurve, Property, QPropertyAnimation, QRectF, QSettings, QSize, QTimer, Qt, QThreadPool, QRunnable, Signal, Slot, QObject
from PySide6.QtGui import QColor, QCloseEvent, QIcon, QLinearGradient, QPainter, QPen, QPixmap, QRadialGradient, QWheelEvent
from PySide6.QtWidgets import QApplication, QAbstractScrollArea, QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGraphicsOpacityEffect, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem, QLineEdit, QMainWindow, QMessageBox, QPushButton, QScrollArea, QScrollBar, QSlider, QSpinBox, QSplitter, QStyle, QStyleOptionSlider, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget
from .assets import app_icon_path, compact_build_id, hero_icon_path, item_icon_path, release_build_id
from .ability_hud import AbilityHudSelectionArbiter, AbilityHudWindow, AbilityTelemetryService
from .ability_profile import AbilityDefinition, get_ability_catalog, get_ability_profile
from .camera import CameraMotionSettings, SmoothCameraController
from .camera_input import CameraInputRouter, KEY_CHOICES
from .camera_modes import CAMERA_TRANSITION_PRESETS, DEFAULT_CUSTOM_TRANSITION, CameraTransitionKind, CameraTransitionSpec, tune_transition
from .dota_profile import DOTA_HERO_NAMES
from .diagnostics import configure_diagnostics, get_logger, is_critical_runtime_error, supported_windows_version
from .launcher import WarcraftLaunchError, WarcraftReplayLauncher, likely_iccup_launchers, likely_warcraft_executables
from .moments import ReplayMoment, ReplayMomentKind, build_replay_moments
from .native_camera import DroneSettings
from .parser import ChatMessage, DotaPlayer, ItemTiming, ReplayReport, invoker_spells_at, parse_replay
from .seeker import AttachResult, SEEK_PROFILES, SeekBackendError, SeekCancelled, SeekMetrics, SeekProfile, SeekProgress, Warcraft126MemoryBackend
from .settings import discover_replays, forget_failed_replay, recover_persistent_settings
LOGGER = get_logger('ui')
APP_NAME = 'Warcraft III Replay Lab'
CAMERA_HERO_SLOT_COUNT = 10

def apply_dark_windows_title_bar(window: QWidget) -> bool:
    if sys.platform != 'win32':
        return False
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = wintypes.HWND(int(window.winId()))
        set_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
        set_attribute.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
        set_attribute.restype = ctypes.c_long
        enabled = wintypes.BOOL(True)
        dark_result = -1
        for attribute in (20, 19):
            dark_result = set_attribute(hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled))
            if dark_result == 0:
                break
        colors = {34: 3153683, 35: 1445384, 36: 15919064}
        for attribute, value in colors.items():
            color = wintypes.DWORD(value)
            set_attribute(hwnd, attribute, ctypes.byref(color), ctypes.sizeof(color))
        redraw_window = ctypes.windll.user32.RedrawWindow
        redraw_window.argtypes = (wintypes.HWND, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT)
        redraw_window.restype = wintypes.BOOL
        redraw_window(hwnd, None, None, 1025)
        return dark_result == 0
    except (AttributeError, OSError, TypeError, ValueError):
        return False
CAMERA_CORE_MACRO_ACTIONS = (('toggle_camera', 'Камера: вкл / выкл', 119), ('follow_toggle', 'Follow: вкл / выкл', 118), ('smart_follow_toggle', 'Smart Follow', 116), ('reset_view', 'Вернуть обзор', 120))
CAMERA_DRONE_MACRO_ACTIONS = (('drone_toggle', 'Fly Drone: вкл / выкл', 66), ('drone_target_lock', 'Drone: захват цели', 78), ('orbit_toggle', 'Orbit: вкл / выкл', 104), ('orbit_reverse', 'Orbit: сменить направление', 98), ('orbit_in', 'Orbit: ближнее кольцо', 103), ('orbit_out', 'Orbit: дальнее кольцо', 105), ('drone_turn_left', 'Drone: поворот влево 90°', 100), ('drone_turn_around', 'Drone: разворот 180°', 101), ('drone_turn_right', 'Drone: поворот вправо 90°', 102), ('drone_height_up', 'Drone: набрать высоту', 97), ('drone_height_down', 'Drone: сбросить высоту', 96))
DRONE_TURN_DEGREES = {'drone_turn_left': 90.0, 'drone_turn_around': 180.0, 'drone_turn_right': -90.0}
ORBIT_RING_LABELS = ('ближняя', 'средняя', 'дальняя')
CAMERA_TRANSITION_ACTIONS = (('transition_dolly_out', 'Dolly Out', 121, CameraTransitionKind.DOLLY_OUT, 'Чистый плавный отъезд назад'), ('transition_crane_up', 'Crane Up', 122, CameraTransitionKind.CRANE_UP, 'Вертикальный операторский подъём'), ('transition_reveal', 'Reveal', 117, CameraTransitionKind.REVEAL, 'Подъём, отдаление и наклон с удержанием героя'), ('transition_push_in', 'Push In', 123, CameraTransitionKind.PUSH_IN, 'Мягкий наезд на текущий кадр'), ('transition_focus_pull', 'Focus Pull', 71, CameraTransitionKind.FOCUS_PULL, 'Отдаление с выбранным героем в центре'), ('transition_custom', 'Свой переход', 84, CameraTransitionKind.CUSTOM, 'Своя дистанция, высота, наклон и длительность'))
CAMERA_HERO_MACRO_ACTIONS = (('hero_slot_1', 'Герой 1', 49), ('hero_slot_2', 'Герой 2', 50), ('hero_slot_3', 'Герой 3', 51), ('hero_slot_4', 'Герой 4', 52), ('hero_slot_5', 'Герой 5', 53), ('hero_slot_6', 'Герой 6', 54), ('hero_slot_7', 'Герой 7', 55), ('hero_slot_8', 'Герой 8', 56), ('hero_slot_9', 'Герой 9', 57), ('hero_slot_10', 'Герой 10', 48))
ABILITY_HUD_MACRO_ACTION = ('ability_hud_toggle', 'Skills HUD: показать / скрыть', 115)
CAMERA_MACRO_ACTIONS = CAMERA_CORE_MACRO_ACTIONS + CAMERA_DRONE_MACRO_ACTIONS + tuple(((action, label, default_key) for action, label, default_key, _, _ in CAMERA_TRANSITION_ACTIONS)) + CAMERA_HERO_MACRO_ACTIONS + (ABILITY_HUD_MACRO_ACTION,)
CAMERA_TRANSITION_BY_ACTION = {action: kind for action, _, _, kind, _ in CAMERA_TRANSITION_ACTIONS}
CAMERA_MOTION_PRESETS = (('Кино · очень плавно', (35.0, 0.55, 1200.0, 1120.0, 2.7, 3.2)), ('Плавно · универсально', (55.0, 1.0, 2200.0, 1760.0, 5.0, 5.0)), ('Быстро · динамично', (90.0, 1.7, 3500.0, 2880.0, 9.0, 8.0)))
DEFAULT_CAMERA_PRESET_INDEX = 1
EDITABLE_CAMERA_TRANSITIONS = tuple((kind for _, _, _, kind, _ in CAMERA_TRANSITION_ACTIONS if kind != CameraTransitionKind.CUSTOM))
MAX_VISIBLE_ITEM_TIMING_WINDOW_MS = 5 * 60 * 1000
RELEASE_BUILD = bool(getattr(sys, '_MEIPASS', None)) or (Path(__file__).resolve().parents[1] / 'release_manifest.json').is_file()

def reset_camera_preferences(settings: QSettings) -> None:
    for group in ('camera_motion', 'camera_shot', 'camera_transition', 'camera_drone', 'camera_macro'):
        settings.remove(group)
    for key in ('camera_preset', 'camera_toggle_hotkey', 'camera_reset_hotkey'):
        settings.remove(key)

def format_time(milliseconds: int | None, *, millis: bool=False) -> str:
    if milliseconds is None:
        return '—'
    milliseconds = max(0, int(milliseconds))
    total_seconds, remainder = divmod(milliseconds, 1000)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        base = f'{hours:d}:{minutes:02d}:{seconds:02d}'
    else:
        base = f'{minutes:02d}:{seconds:02d}'
    return f'{base}.{remainder:03d}' if millis else base

def format_relative_time(milliseconds: int) -> str:
    if milliseconds >= 0:
        return format_time(milliseconds, millis=True)
    return '−' + format_time(-milliseconds, millis=True)

def event_seek_target(event_time_ms: int, preroll_seconds: int) -> int:
    return max(int(event_time_ms) - max(0, min(int(preroll_seconds), 30)) * 1000, 0)

def post_attach_seek_target(backward_launch_armed: bool, pending_seek: int | None) -> int | None:
    if not backward_launch_armed:
        return None
    return pending_seek

def replay_hero_for_selection(players: list[DotaPlayer], player_slot: int | None, hero_rawcode: str | None) -> tuple[int, str, str] | None:
    if player_slot is None or hero_rawcode is None:
        return None
    selected_name = DOTA_HERO_NAMES.get(hero_rawcode)
    if selected_name is None:
        return None
    for player in players:
        if player.slot != player_slot or not player.hero_rawcode:
            continue
        replay_name = player.hero_name or DOTA_HERO_NAMES.get(player.hero_rawcode)
        if hero_rawcode != player.hero_rawcode and selected_name != replay_name:
            return None
        return (player.slot, hero_rawcode, f'{player.name} · {selected_name}')
    return None

def replay_hero_targets(players: list[DotaPlayer]) -> list[tuple[str, tuple[int, str, str]]]:
    targets: list[tuple[str, tuple[int, str, str]]] = []
    for player in players:
        if not player.hero_rawcode:
            continue
        hero = player.hero_name or player.hero_rawcode
        label = f'{player.name} · {hero} · слот {player.slot}'
        targets.append((label, (player.slot, player.hero_rawcode, label)))
    return targets

def number(value: int | None) -> str:
    return '—' if value is None else f'{value:,}'.replace(',', ' ')

def parse_time_input(value: str) -> int:
    normalized = value.strip().replace('.', ':').replace(',', ':')
    if not normalized:
        raise ValueError('empty time')
    if ':' not in normalized:
        if not normalized.isdigit():
            raise ValueError('time must contain digits')
        if len(normalized) <= 2:
            return int(normalized) * 60 * 1000
        if len(normalized) <= 4:
            minutes = int(normalized[:-2])
            seconds = int(normalized[-2:])
            if seconds >= 60:
                raise ValueError('seconds must be below 60')
            return (minutes * 60 + seconds) * 1000
        raise ValueError('use MM:SS or HH:MM:SS')
    parts = normalized.split(':')
    if len(parts) not in (2, 3) or any((not part.isdigit() for part in parts)):
        raise ValueError('use MM:SS or HH:MM:SS')
    numbers = [int(part) for part in parts]
    if len(numbers) == 2:
        minutes, seconds = numbers
        if seconds >= 60:
            raise ValueError('seconds must be below 60')
        total_seconds = minutes * 60 + seconds
    else:
        hours, minutes, seconds = numbers
        if minutes >= 60 or seconds >= 60:
            raise ValueError('minutes and seconds must be below 60')
        total_seconds = hours * 3600 + minutes * 60 + seconds
    return total_seconds * 1000

def visible_item_timing(timing: ItemTiming) -> tuple[str, int | None, str]:
    window_ms = max(timing.latest_game_time_ms - timing.earliest_game_time_ms, 0)
    if timing.precision == 'snapshot-window' and window_ms > MAX_VISIBLE_ITEM_TIMING_WINDOW_MS:
        return ('—', None, 'Точное время покупки не показано: доступные снимки инвентаря расположены слишком далеко друг от друга.')
    if timing.earliest_game_time_ms == timing.latest_game_time_ms:
        label = format_time(timing.latest_game_time_ms)
    elif timing.earliest_game_time_ms <= 0:
        label = f'до {format_time(timing.latest_game_time_ms)}'
    else:
        label = f'{format_time(timing.earliest_game_time_ms)}–{format_time(timing.latest_game_time_ms)}'
    return (label, timing.latest_game_time_ms, 'Время ограничено соседними снимками инвентаря.')

def table_item(text: str, *, alignment: Qt.AlignmentFlag=Qt.AlignmentFlag.AlignCenter, tooltip: str | None=None) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setTextAlignment(alignment)
    if tooltip:
        item.setToolTip(tooltip)
    return item

def scaled_pixmap(path: Path | None, size: int) -> QPixmap | None:
    if path is None:
        return None
    pixmap = QPixmap(str(path))
    if pixmap.isNull():
        return None
    return pixmap.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)

class ParseSignals(QObject):
    ready = Signal(object)
    failed = Signal(str)
    finished = Signal()

class ParseTask(QRunnable):

    def __init__(self, replay_path: Path) -> None:
        super().__init__()
        self.replay_path = replay_path
        self.signals = ParseSignals()

    def run(self) -> None:
        try:
            self.signals.ready.emit(parse_replay(self.replay_path))
        except Exception as exc:
            self.signals.failed.emit(str(exc))
        finally:
            self.signals.finished.emit()

class LaunchSignals(QObject):
    ready = Signal(object)
    failed = Signal(str)
    finished = Signal()

class LaunchTask(QRunnable):

    def __init__(self, launcher: WarcraftReplayLauncher, executable: Path, replay_path: Path, iccup_launcher: Path | None, *, replace_running: bool) -> None:
        super().__init__()
        self.launcher = launcher
        self.executable = executable
        self.replay_path = replay_path
        self.iccup_launcher = iccup_launcher
        self.replace_running = replace_running
        self.signals = LaunchSignals()

    def run(self) -> None:
        try:
            if self.iccup_launcher is not None:
                pid = self.launcher.launch_via_iccup(self.iccup_launcher, self.executable, self.replay_path, replace_running=self.replace_running)
                result = (pid, self.replay_path, 'через iCCup', True)
            else:
                pid = self.launcher.launch(self.executable, self.replay_path, replace_running=self.replace_running)
                result = (pid, self.replay_path, 'напрямую', False)
            self.signals.ready.emit(result)
        except Exception as exc:
            self.signals.failed.emit(str(exc))
        finally:
            self.signals.finished.emit()

class SeekerSignals(QObject):
    operation_started = Signal(str)
    operation_finished = Signal(str)
    scan_progress = Signal(int)
    attached = Signal(object)
    seek_progress = Signal(object)
    seek_metrics = Signal(object)
    seek_finished = Signal(int)
    failed = Signal(str)
    soft_failed = Signal(str)
    cancelled = Signal()
    seek_replaced = Signal(int)

class SeekerService(QObject):

    def __init__(self) -> None:
        super().__init__()
        self.signals = SeekerSignals()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='war3-seeker')
        self._backend: Warcraft126MemoryBackend | None = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._busy = False
        self._operation: str | None = None
        self._pending_seek: tuple[int, SeekProfile, float] | None = None
        self.attached = False

    def _begin(self, operation: str, job: Callable[[], None], *, quiet: bool=False) -> bool:
        with self._lock:
            if self._busy:
                self.signals.failed.emit('Дождись окончания текущей операции или нажми «Стоп».')
                return False
            self._busy = True
            self._operation = operation
        self.signals.operation_started.emit(operation)

        def guarded() -> None:
            try:
                job()
            except SeekCancelled:
                with self._lock:
                    replaced = operation == 'seek' and self._pending_seek is not None
                if not replaced:
                    self.signals.cancelled.emit()
            except (SeekBackendError, OSError, ValueError) as exc:
                (self.signals.soft_failed if quiet else self.signals.failed).emit(str(exc))
            except Exception as exc:
                signal = self.signals.soft_failed if quiet else self.signals.failed
                signal.emit(f'Неожиданная ошибка Seeker: {exc}')
            finally:
                pending_seek: tuple[int, SeekProfile, float] | None = None
                with self._lock:
                    self._busy = False
                    self._operation = None
                    if operation == 'seek':
                        pending_seek = self._pending_seek
                        self._pending_seek = None
                self.signals.operation_finished.emit(operation)
                if pending_seek is not None:
                    self.seek(pending_seek[0], pending_seek[1], requested_at=pending_seek[2])
        self._executor.submit(guarded)
        return True

    def attach_to_warcraft(self, pid: int | None=None, *, quiet: bool=False) -> None:
        self._cancel.clear()

        def job() -> None:
            if self._backend is not None:
                reused = self._backend.reuse_attach(pid)
                if reused is not None:
                    self.attached = True
                    self.signals.attached.emit(reused)
                    return
            backend = Warcraft126MemoryBackend()
            try:
                result = backend.attach(lambda progress: self.signals.scan_progress.emit(int(progress * 100)), self._cancel, pid)
            except Exception:
                backend.close()
                raise
            if self._backend is not None:
                self._backend.close()
            self._backend = backend
            self.attached = True
            self.signals.attached.emit(result)
        self._begin('attach', job, quiet=quiet)

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    def detach(self) -> None:
        if self.busy:
            raise SeekBackendError('Сначала останови текущую перемотку или дождись её окончания.')
        if self._backend is not None:
            self._backend.close()
            self._backend = None
        self.attached = False

    def seek(self, target_replay_time_ms: int, profile: SeekProfile, *, requested_at: float | None=None) -> None:
        request_time = requested_at or time.monotonic()
        with self._lock:
            if self._busy:
                if self._operation == 'seek':
                    self._pending_seek = (target_replay_time_ms, profile, request_time)
                    self._cancel.set()
                    self.signals.seek_replaced.emit(target_replay_time_ms)
                    return
                self.signals.failed.emit('Дождись подключения к Warcraft или нажми «Стоп».')
                return
        self._cancel.clear()

        def job() -> None:
            if self._backend is None or not self.attached:
                raise SeekBackendError('Сначала подключись к Warcraft с уже открытым реплеем.')
            position = self._backend.seek_forward(target_replay_time_ms, self._cancel, self.signals.seek_progress.emit, profile=profile, request_started_at=request_time)
            metrics = self._backend.last_seek_metrics
            if metrics is not None:
                self.signals.seek_metrics.emit(metrics)
            self.signals.seek_finished.emit(position)
        self._begin('seek', job)

    def cancel(self) -> None:
        with self._lock:
            self._pending_seek = None
        self._cancel.set()

    def shutdown(self) -> None:
        with self._lock:
            self._pending_seek = None
        self._cancel.set()

        def close_backend() -> None:
            if self._backend is not None:
                self._backend.close()
                self._backend = None
                self.attached = False
        try:
            self._executor.submit(close_backend)
        except RuntimeError:
            pass
        self._executor.shutdown(wait=False, cancel_futures=False)

class CameraSignals(QObject):
    operation_started = Signal()
    operation_finished = Signal()
    ready = Signal(object)
    state = Signal(object)
    stopped = Signal()
    following = Signal(str)
    smart_follow = Signal(bool)
    hero_slots_ready = Signal(int)
    transition = Signal(str, str, bool)
    drone = Signal(bool)
    drone_target_lock = Signal(bool)
    orbit = Signal(bool, int)
    orbit_ring = Signal(int)
    follow_lost = Signal(str)
    failed = Signal(str)

class CameraMacroSignals(QObject):
    triggered = Signal(str)
    selection_intent = Signal()

class CameraService(QObject):

    def __init__(self, input_router: CameraInputRouter) -> None:
        super().__init__()
        self._input_router = input_router
        self.signals = CameraSignals()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='war3-camera')
        self._prewarm_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='war3-camera-prewarm')
        self._backend: Warcraft126MemoryBackend | None = None
        self._controller: SmoothCameraController | None = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._busy = False
        self._follow_generation = 0

    @property
    def running(self) -> bool:
        return self._controller is not None and self._controller.running

    @property
    def following(self) -> bool:
        return self._controller is not None and self._controller.following

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    @property
    def drone_enabled(self) -> bool:
        return self._controller is not None and self._controller.drone_enabled

    @property
    def drone_target_locked(self) -> bool:
        return self._controller is not None and self._controller.drone_target_locked

    @property
    def orbit_enabled(self) -> bool:
        return self._controller is not None and self._controller.orbit_enabled

    @property
    def orbit_direction(self) -> int:
        controller = self._controller
        return 1 if controller is None else controller.orbit_direction

    @property
    def orbit_ring_index(self) -> int:
        controller = self._controller
        return 1 if controller is None else controller.orbit_ring_index

    @property
    def native_update_hz(self) -> int:
        controller = self._controller
        return 120 if controller is None else controller.native_update_hz

    def update_settings(self, settings: CameraMotionSettings) -> None:
        controller = self._controller
        if controller is None or not controller.running:
            return
        controller.update_settings(move_speed=settings.move_speed, rotation_speed=settings.rotation_speed, zoom_speed=settings.zoom_speed, lift_speed=settings.lift_speed, smoothing=settings.smoothing, follow_smoothing=settings.follow_smoothing)

    def update_drone_settings(self, settings: DroneSettings) -> None:
        controller = self._controller
        if controller is None or not controller.running:
            return
        try:
            controller.update_drone_settings(settings)
        except (SeekBackendError, OSError, ValueError) as exc:
            LOGGER.error('Drone settings update failed: %s', exc)
            self.signals.failed.emit(str(exc))

    def _begin(self, job: Callable[[], None], *, reset_cancel: bool=False) -> bool:
        with self._lock:
            if self._busy:
                self.signals.failed.emit('Дождись окончания подключения камеры.')
                return False
            if reset_cancel:
                self._cancel.clear()
            self._busy = True
        self.signals.operation_started.emit()

        def guarded() -> None:
            try:
                job()
            except SeekCancelled:
                pass
            except (SeekBackendError, OSError, ValueError) as exc:
                LOGGER.error('Camera task failed: %s', exc)
                self.signals.failed.emit(str(exc))
            except Exception:
                LOGGER.exception('Unexpected camera task failure')
                self.signals.failed.emit('Камера остановлена из-за внутренней ошибки.')
            finally:
                with self._lock:
                    self._busy = False
                self.signals.operation_finished.emit()
        self._executor.submit(guarded)
        return True

    def _runtime_failed(self, message: str) -> None:
        LOGGER.error('Camera runtime stopped: %s', message)
        self.signals.failed.emit(message)

        def cleanup() -> None:
            with self._lock:
                self._follow_generation += 1
                controller = self._controller
                backend = self._backend
                self._controller = None
                self._backend = None
                self._input_router.set_camera_process(None)
            if controller is not None:
                controller.stop()
            if backend is not None:
                backend.close()
            self.signals.stopped.emit()
        try:
            self._executor.submit(cleanup)
        except RuntimeError:
            pass

    def start(self, settings: CameraMotionSettings, drone_settings: DroneSettings | None=None) -> None:
        if self.running:
            self.signals.failed.emit('Camera Engine уже запущен.')
            return

        def job() -> None:
            backend = Warcraft126MemoryBackend()
            try:
                backend.attach_process()
                state = backend.attach_camera(self._cancel)
                backend.unlock_camera()
                if self._cancel.is_set():
                    raise SeekCancelled('Camera attachment was cancelled')
                controller = SmoothCameraController(backend, self._input_router, on_error=self._runtime_failed, on_state=self.signals.state.emit, on_follow_lost=self.signals.follow_lost.emit)
                controller.update_settings(move_speed=settings.move_speed, rotation_speed=settings.rotation_speed, zoom_speed=settings.zoom_speed, lift_speed=settings.lift_speed, smoothing=settings.smoothing, follow_smoothing=settings.follow_smoothing)
                if drone_settings is not None:
                    controller.update_drone_settings(drone_settings)
            except Exception:
                backend.close()
                raise
            with self._lock:
                if self._cancel.is_set():
                    controller.stop()
                    backend.close()
                    raise SeekCancelled('Camera attachment was cancelled')
                self._controller = controller
                self._backend = backend
                self._input_router.set_camera_process(backend.process_id)
                controller.start()
            self.signals.ready.emit(state)
        self._begin(job, reset_cancel=True)

    def stop(self) -> None:
        with self._lock:
            self._cancel.set()
            self._follow_generation += 1
            controller = self._controller
            backend = self._backend
            self._controller = None
            self._backend = None
            self._input_router.set_camera_process(None)
        if controller is not None:
            controller.stop()
        if backend is not None:
            backend.close()
        self._input_router.set_camera_process(None)
        self.signals.stopped.emit()

    def follow_selected_unit(self) -> None:
        if self._controller is None or not self._controller.running:
            self.signals.failed.emit('Сначала включи Camera Engine.')
            return
        try:
            with self._lock:
                self._follow_generation += 1
            rawcode = self._controller.follow_selected_unit()
        except (SeekBackendError, OSError, ValueError) as exc:
            self.signals.failed.emit(str(exc))
            return
        self.signals.following.emit(rawcode)
        if self._controller.drone_target_locked:
            self.signals.drone_target_lock.emit(True)
        if self._controller.orbit_enabled:
            self.signals.orbit.emit(True, self._controller.orbit_direction)

    def follow_player_hero(self, player_slot: int, hero_rawcode: str, label: str) -> None:
        controller = self._controller
        if controller is None or not controller.running:
            self.signals.failed.emit('Сначала включи Camera Engine.')
            return
        with self._lock:
            self._follow_generation += 1
            generation = self._follow_generation

        def job() -> None:
            try:
                address, _ = controller.resolve_player_hero(player_slot, hero_rawcode)
                with self._lock:
                    if generation != self._follow_generation or self._controller is not controller:
                        return
                controller.follow_resolved_unit(address)
            except (SeekBackendError, OSError, ValueError) as exc:
                self.signals.failed.emit(str(exc))
                return
            self.signals.following.emit(label)
            if controller.drone_target_locked:
                self.signals.drone_target_lock.emit(True)
            if controller.orbit_enabled:
                self.signals.orbit.emit(True, controller.orbit_direction)
        self._executor.submit(job)

    def prepare_hero_slots(self, slots: list[tuple[int, str]]) -> None:
        controller = self._controller
        if controller is None or not controller.running:
            return
        unique_slots = list(dict.fromkeys(slots))

        def job() -> None:
            ready = 0
            for player_slot, hero_rawcode in unique_slots:
                if self._controller is not controller:
                    return
                try:
                    controller.resolve_player_hero(player_slot, hero_rawcode)
                except (SeekBackendError, OSError, ValueError):
                    continue
                ready += 1
            self.signals.hero_slots_ready.emit(ready)
        try:
            self._prewarm_executor.submit(job)
        except RuntimeError:
            pass

    def clear_follow(self) -> None:
        with self._lock:
            self._follow_generation += 1
        if self._controller is not None:
            self._controller.clear_follow()
        self.signals.smart_follow.emit(False)
        self.signals.drone_target_lock.emit(False)
        self.signals.orbit.emit(False, self.orbit_direction)
        self.signals.follow_lost.emit('')

    def toggle_drone(self) -> None:
        controller = self._controller
        if controller is None or not controller.running:
            self.signals.failed.emit('Сначала включи Camera Engine.')
            return
        try:
            active = controller.toggle_drone()
        except (SeekBackendError, OSError, ValueError) as exc:
            self.signals.failed.emit(str(exc))
            return
        self.signals.drone.emit(active)
        self.signals.drone_target_lock.emit(False)
        self.signals.orbit.emit(False, self.orbit_direction)
        if active:
            self.signals.smart_follow.emit(False)

    def toggle_drone_target_lock(self) -> None:
        controller = self._controller
        if controller is None or not controller.running:
            self.signals.failed.emit('Сначала включи Camera Engine.')
            return
        try:
            active = controller.toggle_drone_target_lock()
        except (SeekBackendError, OSError, ValueError) as exc:
            self.signals.failed.emit(str(exc))
            return
        self.signals.drone_target_lock.emit(active)
        if not active:
            self.signals.orbit.emit(False, self.orbit_direction)

    def toggle_orbit(self) -> None:
        controller = self._controller
        if controller is None or not controller.running:
            self.signals.failed.emit('Сначала включи Camera Engine.')
            return
        try:
            active = controller.toggle_orbit()
        except (SeekBackendError, OSError, ValueError) as exc:
            self.signals.failed.emit(str(exc))
            return
        self.signals.drone.emit(controller.drone_enabled)
        self.signals.drone_target_lock.emit(controller.drone_target_locked)
        self.signals.orbit.emit(active, controller.orbit_direction)
        if active:
            self.signals.smart_follow.emit(False)

    def reverse_orbit(self) -> None:
        controller = self._controller
        if controller is None or not controller.running:
            self.signals.failed.emit('Сначала включи Camera Engine.')
            return
        try:
            direction = controller.reverse_orbit()
        except (SeekBackendError, OSError, ValueError) as exc:
            self.signals.failed.emit(str(exc))
            return
        self.signals.orbit.emit(True, direction)

    def shift_orbit_ring(self, step: int) -> None:
        controller = self._controller
        if controller is None or not controller.running:
            self.signals.failed.emit('Сначала включи Camera Engine.')
            return
        previous_ring = controller.orbit_ring_index
        try:
            ring_index = controller.shift_orbit_ring(step)
        except (SeekBackendError, OSError, ValueError) as exc:
            self.signals.failed.emit(str(exc))
            return
        if ring_index != previous_ring:
            self.signals.orbit_ring.emit(ring_index)

    def turn_drone(self, angle_degrees: float) -> None:
        controller = self._controller
        if controller is None or not controller.running:
            self.signals.failed.emit('Сначала включи Camera Engine.')
            return
        try:
            controller.turn_drone(angle_degrees)
        except (SeekBackendError, OSError, ValueError) as exc:
            self.signals.failed.emit(str(exc))

    def toggle_smart_follow(self) -> None:
        if self._controller is None or not self._controller.running:
            self.signals.failed.emit('Сначала включи Camera Engine.')
            return
        try:
            if self._controller.drone_enabled:
                self._controller.toggle_drone()
                self.signals.drone.emit(False)
                self.signals.drone_target_lock.emit(False)
                self.signals.orbit.emit(False, self.orbit_direction)
            active = self._controller.toggle_smart_follow()
        except (SeekBackendError, OSError, ValueError) as exc:
            self.signals.failed.emit(str(exc))
            return
        self.signals.smart_follow.emit(active)

    def toggle_transition(self, kind: CameraTransitionKind, custom_spec: CameraTransitionSpec | None=None) -> None:
        if self._controller is None or not self._controller.running:
            self.signals.failed.emit('Сначала включи Camera Engine.')
            return
        try:
            subject_label, active = self._controller.toggle_transition(kind, custom_spec)
        except (SeekBackendError, OSError, ValueError) as exc:
            self.signals.failed.emit(str(exc))
            return
        if active:
            self.signals.smart_follow.emit(False)
            self.signals.follow_lost.emit('')
        if not self._controller.drone_enabled:
            self.signals.drone.emit(False)
            self.signals.drone_target_lock.emit(False)
        self.signals.transition.emit(kind.value, subject_label, active)

    def reset_view(self) -> None:
        if self._controller is None or not self._controller.running:
            self.signals.failed.emit('Сначала включи Camera Engine.')
            return
        with self._lock:
            self._follow_generation += 1
        self._controller.reset_view()
        self.signals.smart_follow.emit(False)
        self.signals.drone.emit(False)
        self.signals.drone_target_lock.emit(False)
        self.signals.follow_lost.emit('Стандартный обзор восстановлен · свободная камера активна')

    def shutdown(self) -> None:
        self.stop()
        self._executor.shutdown(wait=False, cancel_futures=False)
        self._prewarm_executor.shutdown(wait=False, cancel_futures=True)

class TimelineSlider(QSlider):

    def __init__(self) -> None:
        super().__init__(Qt.Orientation.Horizontal)
        self._events: list[ReplayMoment] = []
        self.setMinimum(0)
        self.setMaximum(1)
        self.setSingleStep(1000)
        self.setPageStep(10000)
        self.setMinimumHeight(54)

    def set_events(self, events: list[ReplayMoment], duration_game_ms: int) -> None:
        self._events = list(events)
        self.setMinimum(0)
        self.setMaximum(max(duration_game_ms, 1))
        self.update()

    def paintEvent(self, event: object) -> None:
        super().paintEvent(event)
        if self.maximum() <= 0:
            return
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderGroove, self)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(103, 142, 174, 58), 1))
        for index in range(9):
            ratio = index / 8.0
            x = groove.left() + round(ratio * groove.width())
            length = 7 if index % 2 == 0 else 4
            painter.drawLine(x, groove.bottom() + 8, x, groove.bottom() + 8 + length)
        current_ratio = min(max((self.value() - self.minimum()) / max(self.maximum() - self.minimum(), 1), 0.0), 1.0)
        current_x = groove.left() + round(current_ratio * groove.width())
        painter.setPen(QPen(QColor(111, 205, 255, 92), 1))
        painter.drawLine(current_x, groove.top() - 12, current_x, groove.bottom() + 16)
        for moment in self._events:
            ratio = min(max(moment.game_time_ms / self.maximum(), 0), 1)
            x = groove.left() + round(ratio * groove.width())
            color = QColor('#f2c94c') if moment.kind == ReplayMomentKind.FIRST_BLOOD else QColor('#ff6b57') if moment.severity >= 3 else QColor('#55a7ff') if moment.kind == ReplayMomentKind.MULTI_KILL else QColor('#7f8ea3')
            width = 3 if moment.kind != ReplayMomentKind.KILL else 1
            painter.setPen(QPen(color, width))
            painter.drawLine(x, groove.top() - 8, x, groove.bottom() + 8)

class TemporalFingerprint(QWidget):
    BIN_COUNT = 36

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName('temporalFingerprint')
        self.setMinimumSize(210, 40)
        self.setMaximumHeight(40)
        self.setMaximumWidth(270)
        self._bins = [0.0] * self.BIN_COUNT
        self._hot_bins: set[int] = set()
        self._phase = 0.0
        self._active = False
        self._scan_animation = QPropertyAnimation(self, b'scanPhase', self)
        self._scan_animation.setStartValue(0.0)
        self._scan_animation.setEndValue(1.0)
        self._scan_animation.setDuration(2900)
        self._scan_animation.setLoopCount(-1)
        self._scan_animation.setEasingCurve(QEasingCurve.Type.InOutSine)

    def _get_scan_phase(self) -> float:
        return self._phase

    def _set_scan_phase(self, value: float) -> None:
        self._phase = float(value)
        self.update()
    scanPhase = Property(float, _get_scan_phase, _set_scan_phase)

    def clear(self) -> None:
        self._bins = [0.0] * self.BIN_COUNT
        self._hot_bins.clear()
        self.set_active(False)

    def set_events(self, events: list[ReplayMoment], duration_game_ms: int) -> None:
        bins = [0.0] * self.BIN_COUNT
        hot_bins: set[int] = set()
        duration = max(duration_game_ms, 1)
        for moment in events:
            ratio = min(max(moment.game_time_ms / duration, 0.0), 1.0)
            index = min(round(ratio * (self.BIN_COUNT - 1)), self.BIN_COUNT - 1)
            weight = 1.0 + min(max(moment.severity, 0), 3) * 0.45
            bins[index] += weight
            if moment.kind == ReplayMomentKind.FIRST_BLOOD or moment.severity >= 3:
                hot_bins.add(index)
        peak = max(bins, default=0.0)
        self._bins = [value / peak if peak else 0.0 for value in bins]
        self._hot_bins = hot_bins
        self.set_active(bool(events))

    def set_active(self, active: bool) -> None:
        self._active = bool(active)
        if self._active:
            if self._scan_animation.state() != QAbstractAnimation.State.Running:
                self._scan_animation.start()
        else:
            self._scan_animation.stop()
            self._phase = 0.0
            self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        frame = self.rect().adjusted(0, 0, -1, -1)
        painter.setPen(QPen(QColor(42, 86, 108, 170), 1))
        painter.setBrush(QColor(5, 17, 25, 178))
        painter.drawRoundedRect(frame, 8, 8)
        left = 12.0
        right = max(self.width() - 12.0, left + 1.0)
        center_y = self.height() * 0.56
        span = right - left
        painter.setPen(QPen(QColor(72, 130, 157, 62), 1))
        painter.drawLine(round(left), round(center_y), round(right), round(center_y))
        for index in range(5):
            x = left + span * index / 4.0
            painter.drawLine(round(x), round(center_y + 15), round(x), round(center_y + 18))
        if any(self._bins):
            step = span / max(self.BIN_COUNT - 1, 1)
            for index, intensity in enumerate(self._bins):
                if intensity <= 0.0:
                    continue
                x = left + index * step
                amplitude = 4.0 + intensity * 13.0
                color = QColor(239, 185, 76, 210) if index in self._hot_bins else QColor(79, 190, 232, 195)
                halo = QColor(color)
                halo.setAlpha(36)
                painter.setPen(QPen(halo, 4))
                painter.drawLine(round(x), round(center_y - amplitude), round(x), round(center_y + amplitude))
                painter.setPen(QPen(color, 1.4))
                painter.drawLine(round(x), round(center_y - amplitude), round(x), round(center_y + amplitude))
        else:
            painter.setPen(QPen(QColor(74, 113, 135, 82), 1))
            for index in range(12):
                x = left + span * index / 11.0
                painter.drawPoint(round(x), round(center_y))
        if self._active:
            scan_x = left + span * self._phase
            scan = QLinearGradient(scan_x - 25, 0, scan_x + 25, 0)
            scan.setColorAt(0.0, QColor(92, 211, 245, 0))
            scan.setColorAt(0.5, QColor(92, 211, 245, 28))
            scan.setColorAt(1.0, QColor(92, 211, 245, 0))
            painter.fillRect(QRectF(scan_x - 25, 3, 50, self.height() - 6), scan)
            painter.setPen(QPen(QColor(133, 231, 250, 110), 1))
            painter.drawLine(round(scan_x), 7, round(scan_x), self.height() - 7)

class ObsidianSurface(QWidget):

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        base = QLinearGradient(0, 0, self.width(), self.height())
        base.setColorAt(0.0, QColor('#080c12'))
        base.setColorAt(0.55, QColor('#0a1017'))
        base.setColorAt(1.0, QColor('#080d14'))
        painter.fillRect(self.rect(), base)
        glow = QRadialGradient(self.width() * 0.82, self.height() * 0.02, max(self.width() * 0.48, 480.0))
        glow.setColorAt(0.0, QColor(45, 112, 178, 34))
        glow.setColorAt(0.48, QColor(28, 75, 119, 15))
        glow.setColorAt(1.0, QColor(8, 13, 20, 0))
        painter.fillRect(self.rect(), glow)
        ember = QRadialGradient(self.width() * 0.08, self.height() * 0.96, max(self.width() * 0.38, 360.0))
        ember.setColorAt(0.0, QColor(143, 92, 36, 13))
        ember.setColorAt(0.5, QColor(88, 55, 24, 5))
        ember.setColorAt(1.0, QColor(8, 13, 20, 0))
        painter.fillRect(self.rect(), ember)
        painter.setPen(QPen(QColor(87, 127, 164, 12), 1))
        grid_size = 56
        for x in range(0, self.width(), grid_size):
            painter.drawLine(x, 0, x, self.height())
        for y in range(0, self.height(), grid_size):
            painter.drawLine(0, y, self.width(), y)
        painter.setPen(QPen(QColor(88, 137, 171, 8), 1))
        diagonal_span = round(self.height() * 0.32)
        for offset in range(-self.height(), self.width(), 224):
            painter.drawLine(offset, self.height(), offset + diagonal_span, 0)
        center_x = self.width() - 126
        center_y = 70
        painter.setPen(QPen(QColor(92, 157, 214, 22), 1))
        for radius in (44, 72, 102):
            painter.drawEllipse(QRectF(center_x - radius, center_y - radius, radius * 2, radius * 2))
        for index in range(24):
            angle = math.tau * index / 24.0
            inner = 106 if index % 3 == 0 else 110
            outer = 116
            painter.drawLine(round(center_x + math.cos(angle) * inner), round(center_y + math.sin(angle) * inner), round(center_x + math.cos(angle) * outer), round(center_y + math.sin(angle) * outer))
        painter.drawLine(center_x - 116, center_y, center_x + 116, center_y)
        painter.drawLine(center_x, center_y - 116, center_x, center_y + 116)

class SignalPulse(QWidget):
    COLORS = {'idle': QColor('#60758a'), 'busy': QColor('#62b9ff'), 'online': QColor('#6de6b2'), 'error': QColor('#ff7e70')}

    def __init__(self, parent: QWidget | None=None) -> None:
        super().__init__(parent)
        self.setFixedSize(18, 18)
        self._phase = 0.0
        self._state = 'idle'
        self._animation = QPropertyAnimation(self, b'phase', self)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setDuration(1800)
        self._animation.setLoopCount(-1)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._animation.start()

    def _get_phase(self) -> float:
        return self._phase

    def _set_phase(self, value: float) -> None:
        self._phase = float(value)
        self.update()
    phase = Property(float, _get_phase, _set_phase)

    def set_state(self, state: str) -> None:
        self._state = state if state in self.COLORS else 'idle'
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(self.COLORS[self._state])
        radius = 4.0 + self._phase * 3.4
        halo = QColor(color)
        halo.setAlpha(round(74 * (1.0 - self._phase)))
        painter.setPen(QPen(halo, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QRectF(self.width() / 2.0 - radius, self.height() / 2.0 - radius, radius * 2.0, radius * 2.0))
        core = QColor(color)
        core.setAlpha(225)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(core)
        painter.drawEllipse(QRectF(self.width() / 2.0 - 2.3, self.height() / 2.0 - 2.3, 4.6, 4.6))

class TemporalStatusNode(QFrame):

    def __init__(self, title: str, value: str) -> None:
        super().__init__()
        self.setObjectName('temporalStatusNode')
        self.setProperty('signal', 'idle')
        self.setMinimumWidth(132)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 7, 11, 7)
        layout.setSpacing(7)
        self.pulse = SignalPulse(self)
        layout.addWidget(self.pulse)
        labels = QVBoxLayout()
        labels.setSpacing(0)
        title_label = QLabel(title)
        title_label.setObjectName('temporalNodeTitle')
        self.value_label = QLabel(value)
        self.value_label.setObjectName('temporalNodeValue')
        labels.addWidget(title_label)
        labels.addWidget(self.value_label)
        layout.addLayout(labels, 1)

    def set_value(self, value: str, state: str='idle') -> None:
        self.value_label.setText(value)
        self.pulse.set_state(state)
        self.setProperty('signal', state)
        self.style().unpolish(self)
        self.style().polish(self)

class TemporalContextBar(QFrame):

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName('temporalContextBar')
        self._scan_phase = 0.0
        self._active = False
        self._scan_animation = QPropertyAnimation(self, b'scanPhase', self)
        self._scan_animation.setStartValue(0.0)
        self._scan_animation.setEndValue(1.0)
        self._scan_animation.setDuration(3800)
        self._scan_animation.setLoopCount(-1)
        self._scan_animation.setEasingCurve(QEasingCurve.Type.InOutSine)

    def _get_scan_phase(self) -> float:
        return self._scan_phase

    def _set_scan_phase(self, value: float) -> None:
        self._scan_phase = float(value)
        self.update()
    scanPhase = Property(float, _get_scan_phase, _set_scan_phase)

    def set_active(self, active: bool) -> None:
        self._active = bool(active)
        if self._active:
            if self._scan_animation.state() != QAbstractAnimation.State.Running:
                self._scan_animation.start()
        else:
            self._scan_animation.stop()
            self._scan_phase = 0.0
            self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setClipRect(self.rect().adjusted(2, 2, -2, -2))
        painter.setPen(QPen(QColor(101, 155, 196, 24), 1))
        for x in range(24, self.width(), 48):
            painter.drawLine(x, 2, x, 6)
            painter.drawLine(x, self.height() - 7, x, self.height() - 3)
        if not self._active:
            return
        scan_x = self.width() * self._scan_phase
        scan = QLinearGradient(scan_x - 64.0, 0, scan_x + 64.0, 0)
        scan.setColorAt(0.0, QColor(68, 171, 235, 0))
        scan.setColorAt(0.5, QColor(68, 171, 235, 22))
        scan.setColorAt(1.0, QColor(68, 171, 235, 0))
        painter.fillRect(self.rect().adjusted(2, 2, -2, -2), scan)

class InstrumentFrame(QFrame):

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(91, 180, 221, 72), 1))
        margin = 8
        arm = 11
        right = self.width() - margin - 1
        bottom = self.height() - margin - 1
        for x, x_direction in ((margin, 1), (right, -1)):
            painter.drawLine(x, margin, x + arm * x_direction, margin)
            painter.drawLine(x, bottom, x + arm * x_direction, bottom)
        painter.drawLine(margin, margin, margin, margin + arm)
        painter.drawLine(right, margin, right, margin + arm)
        painter.drawLine(margin, bottom, margin, bottom - arm)
        painter.drawLine(right, bottom, right, bottom - arm)

class FloatingScrollBar(QScrollBar):

    def __init__(self, orientation: Qt.Orientation, parent: QWidget | None=None) -> None:
        super().__init__(orientation, parent)
        self.setObjectName('floatingScrollBar')
        self._handle_thickness = 4.0
        self._pressed = False
        if orientation == Qt.Orientation.Vertical:
            self.setFixedWidth(10)
        else:
            self.setFixedHeight(10)
        self._hover_animation = QPropertyAnimation(self, b'handleThickness', self)
        self._hover_animation.setDuration(150)
        self._hover_animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _get_handle_thickness(self) -> float:
        return self._handle_thickness

    def _set_handle_thickness(self, value: float) -> None:
        self._handle_thickness = float(value)
        self.update()
    handleThickness = Property(float, _get_handle_thickness, _set_handle_thickness)

    def _animate_thickness(self, target: float) -> None:
        self._hover_animation.stop()
        self._hover_animation.setStartValue(self._handle_thickness)
        self._hover_animation.setEndValue(target)
        self._hover_animation.start()

    def enterEvent(self, event) -> None:
        self._animate_thickness(7.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if not self._pressed:
            self._animate_thickness(4.0)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        self._pressed = True
        self._animate_thickness(8.0)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        self._pressed = False
        self._animate_thickness(7.0 if self.underMouse() else 4.0)

    def paintEvent(self, event) -> None:
        if self.maximum() <= self.minimum():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        length = self.height() if self.orientation() == Qt.Orientation.Vertical else self.width()
        available = max(length - 4.0, 1.0)
        span = self.maximum() - self.minimum()
        page = max(self.pageStep(), 1)
        handle_length = max(34.0, available * page / (span + page))
        handle_length = min(handle_length, available)
        ratio = (self.value() - self.minimum()) / span
        position = 2.0 + ratio * (available - handle_length)
        thickness = self._handle_thickness
        if self.orientation() == Qt.Orientation.Vertical:
            handle = QRectF((self.width() - thickness) / 2.0, position, thickness, handle_length)
        else:
            handle = QRectF(position, (self.height() - thickness) / 2.0, handle_length, thickness)
        color = QColor('#4d9be8') if self._pressed else QColor('#7798b8') if self.underMouse() else QColor('#486078')
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(handle, thickness / 2.0, thickness / 2.0)

class ReplayLibraryCard(QWidget):

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.setObjectName('replayLibraryCard')
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._highlight_progress = 0.0
        self._hovered = False
        self._selected = False
        self._highlight_animation = QPropertyAnimation(self, b'highlightProgress', self)
        self._highlight_animation.setDuration(175)
        self._highlight_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)
        title = QLabel(path.stem)
        title.setObjectName('replayCardTitle')
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        meta = QLabel(self._metadata(path))
        meta.setObjectName('replayCardMeta')
        meta.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(title)
        layout.addWidget(meta)

    def _get_highlight_progress(self) -> float:
        return self._highlight_progress

    def _set_highlight_progress(self, value: float) -> None:
        self._highlight_progress = float(value)
        self.update()
    highlightProgress = Property(float, _get_highlight_progress, _set_highlight_progress)

    def set_hovered(self, hovered: bool) -> None:
        self._hovered = bool(hovered)
        self._animate_highlight()

    def set_selected(self, selected: bool) -> None:
        self._selected = bool(selected)
        self._animate_highlight()

    def _animate_highlight(self) -> None:
        target = 1.0 if self._hovered or self._selected else 0.0
        self._highlight_animation.stop()
        self._highlight_animation.setStartValue(self._highlight_progress)
        self._highlight_animation.setEndValue(target)
        self._highlight_animation.start()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        progress = self._highlight_progress
        if progress <= 0.001:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        frame = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        wash = QLinearGradient(0, 0, self.width(), 0)
        wash.setColorAt(0.0, QColor(37, 126, 170, round(34 * progress)))
        wash.setColorAt(0.52, QColor(37, 126, 170, round(9 * progress)))
        wash.setColorAt(1.0, QColor(37, 126, 170, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(wash)
        painter.drawRoundedRect(frame, 10, 10)
        border = QColor(80, 173, 218, round(112 * progress))
        painter.setPen(QPen(border, 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(frame, 10, 10)
        sensor = QColor(91, 203, 235, round((210 if self._selected else 150) * progress))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(sensor)
        painter.drawEllipse(QRectF(self.width() - 17, 12, 3.5, 3.5))

    @staticmethod
    def _metadata(path: Path) -> str:
        try:
            size_kib = max(path.stat().st_size / 1024.0, 0.1)
            size = f'{size_kib:.0f} KB'
        except OSError:
            size = 'SIZE UNKNOWN'
        return f'W3G  ·  {size}  ·  {path.parent.name}'

class ReplayLibraryList(QListWidget):

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName('replayLibrary')
        self.setMouseTracking(True)
        self.setSpacing(7)
        self.setUniformItemSizes(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.setVerticalScrollBar(FloatingScrollBar(Qt.Orientation.Vertical, self))
        self.verticalScrollBar().setSingleStep(18)
        self._scroll_animation = QPropertyAnimation(self.verticalScrollBar(), b'value', self)
        self._scroll_animation.setDuration(165)
        self._scroll_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._scroll_target = 0
        self._hovered_item: QListWidgetItem | None = None
        self.currentItemChanged.connect(self._selection_changed)

    def _card_for_item(self, item: QListWidgetItem | None) -> ReplayLibraryCard | None:
        if item is None:
            return None
        widget = self.itemWidget(item)
        return widget if isinstance(widget, ReplayLibraryCard) else None

    def _set_hovered_item(self, item: QListWidgetItem | None) -> None:
        if item is self._hovered_item:
            return
        previous = self._card_for_item(self._hovered_item)
        if previous is not None:
            previous.set_hovered(False)
        self._hovered_item = item
        current = self._card_for_item(item)
        if current is not None:
            current.set_hovered(True)

    def _selection_changed(self, current: QListWidgetItem | None, previous: QListWidgetItem | None) -> None:
        previous_card = self._card_for_item(previous)
        if previous_card is not None:
            previous_card.set_selected(False)
        current_card = self._card_for_item(current)
        if current_card is not None:
            current_card.set_selected(True)

    def mouseMoveEvent(self, event) -> None:
        self._set_hovered_item(self.itemAt(event.position().toPoint()))
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self._set_hovered_item(None)
        super().leaveEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        pixel_delta = event.pixelDelta().y()
        angle_delta = event.angleDelta().y()
        if not pixel_delta and (not angle_delta):
            super().wheelEvent(event)
            return
        scrollbar = self.verticalScrollBar()
        if pixel_delta:
            distance = pixel_delta
        else:
            distance = round(angle_delta / 120.0 * 76)
        base = self._scroll_target if self._scroll_animation.state() == QAbstractAnimation.State.Running else scrollbar.value()
        self._scroll_target = min(max(base - distance, scrollbar.minimum()), scrollbar.maximum())
        self._scroll_animation.stop()
        self._scroll_animation.setStartValue(scrollbar.value())
        self._scroll_animation.setEndValue(self._scroll_target)
        self._scroll_animation.start()
        event.accept()

class StatCard(QFrame):

    def __init__(self, title: str) -> None:
        super().__init__()
        self.setObjectName('statCard')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName('cardTitle')
        self.value_label = QLabel('—')
        self.value_label.setObjectName('cardValue')
        layout.addWidget(title_label)
        layout.addWidget(self.value_label)

    def set_value(self, value: str) -> None:
        self.value_label.setText(value)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        accent = QLinearGradient(14, 0, 70, 0)
        accent.setColorAt(0.0, QColor(77, 176, 218, 118))
        accent.setColorAt(1.0, QColor(77, 176, 218, 0))
        painter.setPen(QPen(accent, 1))
        painter.drawLine(14, 1, 70, 1)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(103, 204, 230, 105))
        painter.drawEllipse(QRectF(self.width() - 18, 12, 3, 3))

class ReplayLabWindow(QMainWindow):

    def __init__(self, settings: QSettings | None=None) -> None:
        super().__init__()
        build_id = compact_build_id(release_build_id())
        self.setWindowTitle(f'{APP_NAME} · {build_id}')
        icon_path = app_icon_path()
        if icon_path is not None:
            self.setWindowIcon(QIcon(str(icon_path)))
        self.resize(1420, 850)
        self.setMinimumSize(QSize(1020, 650))
        self.settings = settings or QSettings('ReplayLab', 'Warcraft3ReplayLab')
        self.settings_recovery = recover_persistent_settings(self.settings)
        stored_roots = self.settings.value('replay_roots', [])
        if isinstance(stored_roots, str):
            stored_roots = [stored_roots]
        self._replay_roots = [Path(str(path)).resolve(strict=False) for path in stored_roots or []]
        stored_library = self.settings.value('replay_library', [])
        if isinstance(stored_library, str):
            stored_library = [stored_library]
        self._manual_replay_paths = {Path(str(path)).resolve(strict=False) for path in stored_library or []}
        self.report: ReplayReport | None = None
        self.current_path: Path | None = None
        self._replay_moments: list[ReplayMoment] = []
        self._report_cache: dict[Path, ReplayReport] = {}
        self._table_focus_mode = False
        self._table_focus_splitter_sizes: list[int] = []
        self._table_focus_animation: QPropertyAnimation | None = None
        self._parse_task: ParseTask | None = None
        self._launch_task: LaunchTask | None = None
        self._last_requested_replay_time: int | None = None
        self._pending_backward_seek: int | None = None
        self._backward_launch_armed = False
        self._pending_backward_profile: SeekProfile | None = None
        self._pending_backward_deadline = 0.0
        self._pending_attach_attempt = False
        self._auto_attach_pid: int | None = None
        self._auto_attach_deadline = 0.0
        self.seeker = SeekerService()
        self.camera_macro_signals = CameraMacroSignals()
        self.camera_input = CameraInputRouter(self.camera_macro_signals.triggered.emit, self.camera_macro_signals.selection_intent.emit)
        self.camera_service = CameraService(self.camera_input)
        self.ability_hud_service = AbilityTelemetryService()
        self.ability_hud_window = AbilityHudWindow()
        self._ability_hud_display_target: tuple[int, str] | None = None
        self._ability_hud_requested_target: tuple[int, str] | None = None
        self._ability_hud_address_cache: dict[tuple[int, str], int] = {}
        self._ability_hud_selection = AbilityHudSelectionArbiter()
        self.launcher = WarcraftReplayLauncher()
        self._build_ui()
        self._install_floating_scrollbars()
        self._wire_seeker()
        self._wire_camera()
        self._wire_ability_hud()
        self._apply_style()
        QTimer.singleShot(0, self._apply_native_window_frame)
        self.camera_macro_signals.triggered.connect(self._camera_macro_triggered)
        self.camera_macro_signals.selection_intent.connect(self._ability_hud_pointer_selection)
        self._camera_input_poll = QTimer(self)
        self._camera_input_poll.setInterval(25)
        self._camera_input_poll.timeout.connect(self.camera_input.poll_passthrough_actions)
        self._camera_input_ready = False
        try:
            self.camera_input.start()
        except SeekBackendError as exc:
            self.camera_start_button.setEnabled(False)
            QTimer.singleShot(0, lambda message=str(exc): self._camera_error(message))
        else:
            self._camera_input_ready = True
            self._camera_input_poll.start()
        indexed_paths = set(self._manual_replay_paths)
        for replay_root in self._replay_roots:
            indexed_paths.update(discover_replays(replay_root))
        for path in sorted(indexed_paths, key=lambda replay: (replay.stat().st_mtime if replay.is_file() else 0.0, str(replay).casefold()), reverse=True):
            if path.is_file():
                self._add_replay(path, persist=False)
        startup_replay: Path | None = None
        last_path = self.settings.value('last_replay', '')
        if last_path and Path(str(last_path)).is_file():
            startup_replay = Path(str(last_path)).resolve()
            self._add_replay(startup_replay, select=True, persist=False)
        elif self.replay_list.count():
            self.replay_list.setCurrentRow(0)
            selected_item = self.replay_list.currentItem()
            if selected_item is not None:
                startup_replay = Path(str(selected_item.data(Qt.ItemDataRole.UserRole))).resolve()
        self.launch_replay_button.setEnabled(self.replay_list.currentItem() is not None)
        self._save_replay_library()
        if startup_replay is not None:
            self.status_label.setText(f'Выбран {startup_replay.name}. Открой его вручную — ReplayLab не повторяет операции прошлого сеанса автоматически.')
        elif self.settings_recovery.repaired:
            self.status_label.setText('ReplayLab очистил незавершённое состояние прошлого сеанса.')

    def _apply_native_window_frame(self) -> None:
        self._native_title_bar_dark = apply_dark_windows_title_bar(self)

    def _build_ui(self) -> None:
        central = ObsidianSurface()
        central.setObjectName('obsidianSurface')
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(14)
        top_bar = QFrame()
        top_bar.setObjectName('topBar')
        toolbar = QHBoxLayout(top_bar)
        toolbar.setContentsMargins(14, 9, 12, 9)
        toolbar.setSpacing(10)
        self.brand_mark = QLabel()
        self.brand_mark.setObjectName('labMark')
        self.brand_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.brand_mark.setFixedSize(42, 42)
        brand_icon = scaled_pixmap(app_icon_path(), 38)
        if brand_icon is not None:
            self.brand_mark.setPixmap(brand_icon)
        toolbar.addWidget(self.brand_mark)
        brand = QVBoxLayout()
        brand.setSpacing(0)
        title = QLabel('ReplayLab')
        title.setObjectName('appTitle')
        subtitle = QLabel('TEMPORAL REPLAY OBSERVATORY')
        subtitle.setObjectName('appSubtitle')
        brand.addWidget(title)
        brand.addWidget(subtitle)
        toolbar.addLayout(brand)
        build_id = compact_build_id(release_build_id())
        self.lab_chip = QLabel(f'TEMPORAL OBSERVATORY  ·  {build_id}')
        self.lab_chip.setObjectName('labChip')
        toolbar.addWidget(self.lab_chip)
        system_state = QHBoxLayout()
        system_state.setSpacing(3)
        self.system_pulse = SignalPulse()
        self.system_state_label = QLabel('SYSTEM STANDBY')
        self.system_state_label.setObjectName('systemState')
        system_state.addWidget(self.system_pulse)
        system_state.addWidget(self.system_state_label)
        toolbar.addLayout(system_state)
        toolbar.addStretch()
        self.open_file_button = QPushButton('Добавить реплеи')
        self.open_file_button.setProperty('role', 'primary')
        self.open_folder_button = QPushButton('Открыть папку')
        self.open_folder_button.setProperty('role', 'secondary')
        self.export_button = QPushButton('Экспорт JSON')
        self.export_button.setProperty('role', 'ghost')
        self.export_button.setEnabled(False)
        toolbar.addWidget(self.open_file_button)
        toolbar.addWidget(self.open_folder_button)
        if not RELEASE_BUILD:
            toolbar.addWidget(self.export_button)
        root.addWidget(top_bar)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter = splitter
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter, 1)
        sidebar = QFrame()
        self.sidebar = sidebar
        sidebar.setObjectName('sidebar')
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(12, 14, 12, 12)
        sidebar_layout.setSpacing(10)
        library_header = QHBoxLayout()
        library_title = QLabel('REPLAY ARCHIVE')
        library_title.setObjectName('sectionEyebrow')
        self.library_count_label = QLabel('0 RECORDS')
        self.library_count_label.setObjectName('sectionCount')
        library_header.addWidget(library_title)
        library_header.addStretch()
        library_header.addWidget(self.library_count_label)
        sidebar_layout.addLayout(library_header)
        self.replay_list = ReplayLibraryList()
        self.replay_list.setMinimumWidth(230)
        self.replay_list.setMaximumWidth(360)
        sidebar_layout.addWidget(self.replay_list, 1)
        self.launch_replay_button = QPushButton('Открыть в Warcraft')
        self.launch_replay_button.setProperty('role', 'secondary')
        self.launch_paths_button = QPushButton('Настроить запуск')
        self.launch_paths_button.setProperty('role', 'primary')
        self.launch_replay_button.setEnabled(False)
        self.auto_launch_checkbox = QCheckBox('Запускать при выборе')
        self.auto_launch_checkbox.setChecked(str(self.settings.value('auto_launch_replay', 'false')).lower() == 'true')
        self.auto_launch_checkbox.setToolTip('При выборе другого реплея Warcraft будет мягко перезапущен с этим файлом.')
        sidebar_layout.addWidget(self.launch_replay_button)
        sidebar_layout.addWidget(self.launch_paths_button)
        sidebar_layout.addWidget(self.auto_launch_checkbox)
        splitter.addWidget(sidebar)
        content = QWidget()
        content.setObjectName('contentArea')
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(10)
        content_layout.addWidget(self._build_temporal_context_bar())
        self.camera_macro_combos: dict[str, QComboBox] = {}
        self._camera_binding_values: dict[str, int] = {}
        self.tabs = QTabWidget()
        self.tabs.setObjectName('productTabs')
        self.tabs.tabBar().setObjectName('productTabBar')
        self.stats_tab = self._build_stats_tab()
        self.moments_tab = self._build_moments_tab()
        self.camera_tab = self._build_camera_tab()
        self.tabs.addTab(self.stats_tab, 'Статистика')
        self.tabs.addTab(self.moments_tab, 'Просмотр')
        self.tabs.addTab(self.camera_tab, 'Съёмка')
        self.tabs.currentChanged.connect(self._product_direction_changed)
        content_layout.addWidget(self.tabs, 1)
        content_layout.addWidget(self._build_transport_bar())
        splitter.addWidget(content)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([270, 1100])
        diagnostic_rail = QFrame()
        diagnostic_rail.setObjectName('diagnosticRail')
        diagnostic_layout = QHBoxLayout(diagnostic_rail)
        diagnostic_layout.setContentsMargins(10, 5, 10, 5)
        diagnostic_layout.setSpacing(9)
        diagnostic_title = QLabel('SESSION LOG')
        diagnostic_title.setObjectName('diagnosticTitle')
        diagnostic_layout.addWidget(diagnostic_title)
        self.status_label = QLabel('Выбери .w3g — отчёт появится здесь.')
        self.status_label.setObjectName('statusLabel')
        diagnostic_layout.addWidget(self.status_label, 1)
        diagnostic_mode = QLabel('LOCAL  /  FAIL-CLOSED')
        diagnostic_mode.setObjectName('diagnosticMode')
        diagnostic_layout.addWidget(diagnostic_mode)
        root.addWidget(diagnostic_rail)
        self.setCentralWidget(central)
        self.open_file_button.clicked.connect(self.open_file)
        self.open_folder_button.clicked.connect(self.open_folder)
        self.export_button.clicked.connect(self.export_json)
        self.replay_list.itemClicked.connect(self._activate_replay)
        self.launch_replay_button.clicked.connect(self._launch_current_replay)
        self.launch_paths_button.clicked.connect(self._configure_launch_paths)
        self.auto_launch_checkbox.toggled.connect(lambda checked: self.settings.setValue('auto_launch_replay', checked))

    def _build_temporal_context_bar(self) -> QWidget:
        self.temporal_context = TemporalContextBar()
        layout = QHBoxLayout(self.temporal_context)
        layout.setContentsMargins(14, 8, 12, 8)
        layout.setSpacing(9)
        specimen = QVBoxLayout()
        specimen.setSpacing(1)
        specimen_title = QLabel('ACTIVE REPLAY')
        specimen_title.setObjectName('specimenEyebrow')
        self.specimen_name_label = QLabel('NO REPLAY SELECTED')
        self.specimen_name_label.setObjectName('specimenName')
        self.specimen_meta_label = QLabel('ADD A .W3G RECORD TO BEGIN TEMPORAL RECONSTRUCTION')
        self.specimen_meta_label.setObjectName('specimenMeta')
        specimen.addWidget(specimen_title)
        specimen.addWidget(self.specimen_name_label)
        specimen.addWidget(self.specimen_meta_label)
        layout.addLayout(specimen, 1)
        self.fingerprint_panel = QWidget()
        self.fingerprint_panel.setObjectName('fingerprintPanel')
        fingerprint_layout = QVBoxLayout(self.fingerprint_panel)
        fingerprint_layout.setContentsMargins(0, 0, 0, 0)
        fingerprint_layout.setSpacing(2)
        fingerprint_title = QLabel('TEMPORAL SIGNATURE')
        fingerprint_title.setObjectName('fingerprintTitle')
        self.temporal_fingerprint = TemporalFingerprint()
        fingerprint_layout.addWidget(fingerprint_title)
        fingerprint_layout.addWidget(self.temporal_fingerprint)
        layout.addWidget(self.fingerprint_panel)
        self.temporal_source_node = TemporalStatusNode('SOURCE', 'NO SIGNAL')
        self.temporal_model_node = TemporalStatusNode('MODEL', 'STANDBY')
        self.temporal_runtime_node = TemporalStatusNode('WARCRAFT LINK', 'OFFLINE')
        self.temporal_runtime_node.set_value('OFFLINE', 'idle')
        layout.addWidget(self.temporal_source_node)
        layout.addWidget(self.temporal_model_node)
        layout.addWidget(self.temporal_runtime_node)
        return self.temporal_context

    def _set_system_state(self, text: str, state: str='idle') -> None:
        self.system_state_label.setText(text)
        self.system_state_label.setProperty('signal', state)
        self.system_pulse.set_state(state)
        self.system_state_label.style().unpolish(self.system_state_label)
        self.system_state_label.style().polish(self.system_state_label)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, 'fingerprint_panel'):
            self.fingerprint_panel.setVisible(self.width() >= 1280)
        if hasattr(self, 'lab_chip'):
            self.lab_chip.setVisible(self.width() >= 1180)
        compact_height = self.height() < 720
        if hasattr(self, 'player_detail'):
            self.player_detail.setVisible(not compact_height and (not self._table_focus_mode))
        if hasattr(self, 'seek_metrics_label'):
            self.seek_metrics_label.setVisible(not compact_height)
        if hasattr(self, 'seek_status'):
            self.seek_status.setVisible(not compact_height)
        if hasattr(self, 'timeline'):
            self.timeline.setMinimumHeight(44 if compact_height else 54)
        if hasattr(self, 'temporal_transport'):
            self.temporal_transport.setMaximumHeight(135 if compact_height else 16777215)

    def _install_floating_scrollbars(self) -> None:
        for area in self.findChildren(QAbstractScrollArea):
            vertical = area.verticalScrollBar()
            if not isinstance(vertical, FloatingScrollBar):
                single_step = vertical.singleStep()
                replacement = FloatingScrollBar(Qt.Orientation.Vertical, area)
                area.setVerticalScrollBar(replacement)
                replacement.setSingleStep(single_step)
            horizontal = area.horizontalScrollBar()
            if not isinstance(horizontal, FloatingScrollBar):
                single_step = horizontal.singleStep()
                replacement = FloatingScrollBar(Qt.Orientation.Horizontal, area)
                area.setHorizontalScrollBar(replacement)
                replacement.setSingleStep(single_step)

    def _build_stats_tab(self) -> QWidget:
        tab = QWidget()
        outer_layout = QVBoxLayout(tab)
        outer_layout.setContentsMargins(0, 10, 0, 0)
        self.stats_cards = QWidget()
        self.stats_cards.setObjectName('statsCards')
        cards = QHBoxLayout(self.stats_cards)
        cards.setContentsMargins(0, 0, 0, 0)
        self.map_card = StatCard('КАРТА')
        self.duration_card = StatCard('ИГРОВОЕ ВРЕМЯ')
        self.kills_card = StatCard('УБИЙСТВА')
        self.moments_card = StatCard('СЕРИИ')
        for card in (self.map_card, self.duration_card, self.kills_card, self.moments_card):
            cards.addWidget(card, 1)
        outer_layout.addWidget(self.stats_cards)
        self.stats_sections = QTabWidget()
        self.stats_sections.setObjectName('sectionTabs')
        self.stats_sections.tabBar().setObjectName('sectionTabBar')
        players_page = QWidget()
        layout = QVBoxLayout(players_page)
        layout.setContentsMargins(0, 8, 0, 0)
        self.table_focus_rail = QFrame()
        self.table_focus_rail.setObjectName('tableFocusRail')
        focus_layout = QHBoxLayout(self.table_focus_rail)
        focus_layout.setContentsMargins(15, 10, 12, 10)
        focus_layout.setSpacing(10)
        focus_identity = QVBoxLayout()
        focus_identity.setSpacing(1)
        focus_eyebrow = QLabel('PLAYER MATRIX  /  FULL VIEW')
        focus_eyebrow.setObjectName('focusEyebrow')
        self.table_focus_name = QLabel('NO REPLAY SELECTED')
        self.table_focus_name.setObjectName('focusTitle')
        focus_identity.addWidget(focus_eyebrow)
        focus_identity.addWidget(self.table_focus_name)
        focus_layout.addLayout(focus_identity)
        focus_layout.addStretch()
        self.table_focus_meta = QLabel('00 PLAYERS  ·  17 COLUMNS')
        self.table_focus_meta.setObjectName('focusMeta')
        focus_layout.addWidget(self.table_focus_meta)
        self.table_focus_exit_button = QPushButton('Вернуться к обзору')
        self.table_focus_exit_button.setProperty('role', 'primary')
        self.table_focus_exit_button.setProperty('density', 'compact')
        focus_layout.addWidget(self.table_focus_exit_button)
        self.table_focus_rail.setVisible(False)
        layout.addWidget(self.table_focus_rail)
        detail = QFrame()
        self.player_detail = detail
        detail.setObjectName('playerDetail')
        detail_layout = QHBoxLayout(detail)
        detail_layout.setContentsMargins(12, 9, 12, 9)
        detail_layout.setSpacing(10)
        self.player_side_signal = QFrame()
        self.player_side_signal.setObjectName('sideSignal')
        self.player_side_signal.setProperty('side', 'unknown')
        self.player_side_signal.setFixedWidth(3)
        self.player_side_signal.setMinimumHeight(60)
        detail_layout.addWidget(self.player_side_signal)
        self.hero_portrait = QLabel('?')
        self.hero_portrait.setObjectName('heroPortrait')
        self.hero_portrait.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hero_portrait.setFixedSize(66, 66)
        detail_layout.addWidget(self.hero_portrait)
        identity_layout = QVBoxLayout()
        identity_layout.setSpacing(2)
        identity_header = QHBoxLayout()
        identity_header.setSpacing(8)
        self.detail_player_name = QLabel('Выбери игрока')
        self.detail_player_name.setObjectName('playerName')
        self.player_identity_badge = QLabel('SLOT --  ·  NID --')
        self.player_identity_badge.setObjectName('identityBadge')
        identity_header.addWidget(self.detail_player_name)
        identity_header.addWidget(self.player_identity_badge)
        identity_header.addStretch()
        self.detail_hero_name = QLabel('Портрет и финальная сборка')
        self.detail_hero_name.setObjectName('playerHero')
        self.detail_summary = QLabel('—')
        self.detail_summary.setObjectName('playerMeta')
        identity_layout.addLayout(identity_header)
        identity_layout.addWidget(self.detail_hero_name)
        identity_layout.addWidget(self.detail_summary)
        detail_layout.addLayout(identity_layout)
        detail_layout.addStretch()
        inventory_layout = QVBoxLayout()
        self.inventory_title = QLabel('ФИНАЛЬНЫЙ ИНВЕНТАРЬ')
        self.inventory_title.setObjectName('cardTitle')
        inventory_header = QHBoxLayout()
        inventory_header.setSpacing(8)
        inventory_header.addWidget(self.inventory_title)
        inventory_header.addStretch()
        self.inventory_evidence = QLabel('NO SIGNAL')
        self.inventory_evidence.setObjectName('evidenceBadge')
        self.inventory_evidence.setProperty('evidence', 'none')
        inventory_header.addWidget(self.inventory_evidence)
        inventory_layout.addLayout(inventory_header)
        slots_layout = QHBoxLayout()
        slots_layout.setSpacing(7)
        self.inventory_slots: list[QLabel] = []
        for _ in range(6):
            slot = QLabel('—')
            slot.setObjectName('itemSlot')
            slot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            slot.setFixedSize(48, 48)
            self.inventory_slots.append(slot)
            slots_layout.addWidget(slot)
        inventory_layout.addLayout(slots_layout)
        detail_layout.addLayout(inventory_layout)
        layout.addWidget(detail)
        self.stats_table = QTableWidget(0, 17)
        self.stats_table.setHorizontalHeaderLabels(['Игрок', 'Герой', 'Сторона', 'Итог', 'K', 'D', 'A', 'Крипы', 'Денаи', 'Нейтралы', 'Золото', 'Инвентарь', 'Net worth', 'APM сред.', 'APM пик', 'Пик на', 'Башни / Rax'])
        self._configure_table(self.stats_table)
        self.stats_table.setIconSize(QSize(32, 32))
        self.stats_table.verticalHeader().setDefaultSectionSize(40)
        self.stats_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.stats_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.stats_table)
        self.stats_table.itemSelectionChanged.connect(self._stats_selection_changed)
        self.stats_sections.addTab(players_page, 'Игроки')
        self.stats_sections.addTab(self._build_chat_page(), 'Чат')
        self.full_table_button = QPushButton('Развернуть таблицу')
        self.full_table_button.setProperty('role', 'ghost')
        self.full_table_button.setProperty('density', 'compact')
        self.full_table_button.setToolTip('Открыть встроенный Focus View со всеми игроками матча.')
        self.full_table_button.setEnabled(False)
        self.stats_sections.setCornerWidget(self.full_table_button, Qt.Corner.TopRightCorner)
        self.full_table_button.clicked.connect(self._toggle_table_focus)
        self.table_focus_exit_button.clicked.connect(lambda: self._set_table_focus_mode(False))
        self.stats_sections.currentChanged.connect(self._statistics_section_changed)
        outer_layout.addWidget(self.stats_sections, 1)
        return tab

    def _product_direction_changed(self, index: int) -> None:
        if self._table_focus_mode and self.tabs.widget(index) is not self.stats_tab:
            self._set_table_focus_mode(False)

    def _statistics_section_changed(self, index: int) -> None:
        if self._table_focus_mode and index != 0:
            self._set_table_focus_mode(False)
        self.full_table_button.setVisible(index == 0)

    def _toggle_table_focus(self) -> None:
        self._set_table_focus_mode(not self._table_focus_mode)

    def _set_table_focus_mode(self, active: bool) -> None:
        active = bool(active and self.report is not None)
        if active == self._table_focus_mode:
            return
        self._table_focus_mode = active
        if active:
            self._table_focus_splitter_sizes = self.main_splitter.sizes()
            self.full_table_button.setVisible(False)
            self.sidebar.setVisible(False)
            self.temporal_context.setVisible(False)
            self.stats_cards.setVisible(False)
            self.player_detail.setVisible(False)
            self.temporal_transport.setVisible(False)
            self.tabs.tabBar().setVisible(False)
            self.stats_sections.tabBar().setVisible(False)
            self.table_focus_rail.setVisible(True)
            self.stats_table.setFocus()
        else:
            self.sidebar.setVisible(True)
            self.temporal_context.setVisible(True)
            self.stats_cards.setVisible(True)
            self.player_detail.setVisible(self.height() >= 720)
            self.temporal_transport.setVisible(True)
            self.tabs.tabBar().setVisible(True)
            self.stats_sections.tabBar().setVisible(True)
            self.table_focus_rail.setVisible(False)
            self.full_table_button.setVisible(self.stats_sections.currentIndex() == 0)
            if self._table_focus_splitter_sizes:
                QTimer.singleShot(0, lambda sizes=list(self._table_focus_splitter_sizes): self.main_splitter.setSizes(sizes))
        self._animate_table_focus_transition()

    def _animate_table_focus_transition(self) -> None:
        if self._table_focus_animation is not None:
            self._table_focus_animation.stop()
        self.stats_tab.setGraphicsEffect(None)
        effect = QGraphicsOpacityEffect(self.stats_tab)
        effect.setOpacity(0.72)
        self.stats_tab.setGraphicsEffect(effect)
        animation = QPropertyAnimation(effect, b'opacity', self)
        animation.setStartValue(0.72)
        animation.setEndValue(1.0)
        animation.setDuration(180)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)

        def clear_effect() -> None:
            if self.stats_tab.graphicsEffect() is effect:
                self.stats_tab.setGraphicsEffect(None)
        animation.finished.connect(clear_effect)
        self._table_focus_animation = animation
        animation.start()

    def _build_chat_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        controls = QHBoxLayout()
        self.chat_filter = QComboBox()
        self.chat_filter.addItem('Матч', 'match')
        self.chat_filter.addItem('Все сообщения', 'all')
        self.chat_filter.addItem('До старта', 'pregame')
        self.chat_filter.addItem('Общий чат', 'all_chat')
        self.chat_filter.addItem('Союзники', 'allies')
        self.chat_search = QLineEdit()
        self.chat_search.setPlaceholderText('Поиск по игроку или сообщению')
        self.chat_search.setClearButtonEnabled(True)
        controls.addWidget(QLabel('ПОКАЗАТЬ'))
        controls.addWidget(self.chat_filter)
        controls.addWidget(self.chat_search, 1)
        layout.addLayout(controls)
        self.chat_table = QTableWidget(0, 4)
        self.chat_table.setHorizontalHeaderLabels(['Время', 'Канал', 'Игрок', 'Сообщение'])
        self._configure_table(self.chat_table)
        self.chat_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.chat_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.chat_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.chat_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.chat_table.verticalHeader().setDefaultSectionSize(36)
        layout.addWidget(self.chat_table, 1)
        self.chat_filter.currentIndexChanged.connect(self._refresh_chat)
        self.chat_search.textChanged.connect(self._refresh_chat)
        self.chat_table.cellDoubleClicked.connect(self._chat_message_double_clicked)
        return page

    def _build_moments_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 10, 0, 0)
        self.view_sections = QTabWidget()
        self.view_sections.setObjectName('viewSections')
        self.view_sections.tabBar().setObjectName('sectionTabBar')
        self.moments_page = self._build_moments_page()
        self.ability_hud_page = self._build_ability_hud_page()
        self.view_sections.addTab(self.moments_page, 'События')
        self.view_sections.addTab(self.ability_hud_page, 'HUD и оверлеи')
        layout.addWidget(self.view_sections)
        return tab

    def _build_moments_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 10, 0, 0)
        controls = QHBoxLayout()
        self.moment_filter = QComboBox()
        self.moment_filter.addItem('Все события', 'all')
        self.moment_filter.addItem('Обычные фраги', 'kills')
        self.moment_filter.addItem('First Blood и серии', 'highlights')
        self.seek_preroll = QSpinBox()
        self.seek_preroll.setRange(0, 30)
        self.seek_preroll.setSuffix(' сек')
        self.seek_preroll.setValue(max(0, min(30, int(self.settings.value('seek_preroll_seconds', 10)))))
        self.seek_preroll.setToolTip('ReplayLab начнёт воспроизведение раньше события, чтобы успеть подготовить кадр.')
        controls.addWidget(QLabel('СОБЫТИЯ'))
        controls.addWidget(self.moment_filter)
        controls.addStretch()
        controls.addWidget(QLabel('НАЧАТЬ ЗА'))
        controls.addWidget(self.seek_preroll)
        layout.addLayout(controls)
        self.moments_hint = QLabel('Серый — фраг · жёлтый — First Blood · синий/красный — серии.')
        self.moments_hint.setObjectName('hint')
        layout.addWidget(self.moments_hint)
        self.moments_table = QTableWidget(0, 5)
        self.moments_table.setHorizontalHeaderLabels(['Событие', 'Старт', 'Тип', 'Игрок и герой', 'Жертвы'])
        self._configure_table(self.moments_table)
        self.moments_table.setIconSize(QSize(32, 32))
        self.moments_table.verticalHeader().setDefaultSectionSize(40)
        for column in (0, 1, 2):
            self.moments_table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.moments_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.moments_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.moments_table)
        self.moment_filter.currentIndexChanged.connect(self._refresh_moments)
        self.seek_preroll.valueChanged.connect(self._seek_preroll_changed)
        self.moments_table.cellClicked.connect(self._moment_clicked)
        return page

    def _build_ability_hud_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)
        kicker = QLabel('OBSERVER DISPLAY LAYER')
        kicker.setObjectName('sectionEyebrow')
        layout.addWidget(kicker)
        title = QLabel('Skills HUD')
        title.setObjectName('sectionTitle')
        layout.addWidget(title)
        intro = QLabel('Живые способности, уровни и кулдауны выбранного героя. Слой работает поверх Warcraft и не зависит от Camera Engine.')
        intro.setObjectName('playerMeta')
        intro.setWordWrap(True)
        layout.addWidget(intro)
        controls = QFrame()
        controls.setObjectName('transitionCard')
        controls_layout = QGridLayout(controls)
        controls_layout.setContentsMargins(14, 12, 14, 12)
        controls_layout.setHorizontalSpacing(10)
        controls_layout.setVerticalSpacing(10)
        controls_layout.addWidget(QLabel('Герой в HUD'), 0, 0)
        self.ability_hud_player = QComboBox()
        self.ability_hud_player.setMinimumWidth(260)
        controls_layout.addWidget(self.ability_hud_player, 0, 1, 1, 2)
        self.ability_hud_start_button = QPushButton('Включить Skills HUD')
        self.ability_hud_start_button.setProperty('role', 'primary')
        self.ability_hud_start_button.setEnabled(False)
        controls_layout.addWidget(self.ability_hud_start_button, 1, 0)
        self.ability_hud_stop_button = QPushButton('Выключить')
        self.ability_hud_stop_button.setEnabled(False)
        controls_layout.addWidget(self.ability_hud_stop_button, 1, 1)
        self.ability_hud_follow_selection = QCheckBox('Следить за выбором героя в Warcraft')
        self.ability_hud_follow_selection.setChecked(str(self.settings.value('ability_hud_follow_selection', 'true')).lower() == 'true')
        self.ability_hud_follow_selection.setToolTip('Клик по герою или клавиша слота 1–0 переключает выбранного героя и Skills HUD.')
        controls_layout.addWidget(self.ability_hud_follow_selection, 2, 0, 1, 3)
        controls_layout.addWidget(QLabel('Показать / скрыть'), 3, 0)
        self.ability_hud_hotkey = QComboBox()
        self.ability_hud_hotkey.setMinimumWidth(110)
        self._setup_camera_macro_combo(self.ability_hud_hotkey, ABILITY_HUD_MACRO_ACTION[0], ABILITY_HUD_MACRO_ACTION[2])
        controls_layout.addWidget(self.ability_hud_hotkey, 3, 1)
        controls_layout.setColumnStretch(2, 1)
        layout.addWidget(controls)
        self.ability_hud_status = QLabel('Открой распознанный replay в Warcraft и выбери героя.')
        self.ability_hud_status.setObjectName('connectionOffline')
        self.ability_hud_status.setWordWrap(True)
        layout.addWidget(self.ability_hud_status)
        layout.addStretch()
        self.ability_hud_start_button.clicked.connect(self._start_ability_hud)
        self.ability_hud_stop_button.clicked.connect(self.ability_hud_service.stop)
        self.ability_hud_player.currentIndexChanged.connect(lambda _index: self._ability_hud_target_changed())
        self.ability_hud_follow_selection.toggled.connect(self._ability_hud_follow_selection_changed)
        return page

    def _build_transport_bar(self) -> QWidget:
        transport = InstrumentFrame()
        self.temporal_transport = transport
        transport.setObjectName('temporalTransport')
        layout = QVBoxLayout(transport)
        layout.setContentsMargins(14, 9, 14, 9)
        layout.setSpacing(5)
        coordinate_header = QHBoxLayout()
        coordinate_header.setSpacing(10)
        coordinate_title = QLabel('TEMPORAL COORDINATE')
        coordinate_title.setObjectName('coordinateTitle')
        coordinate_header.addWidget(coordinate_title)
        self.temporal_position_label = QLabel('T+00:00.000  /  000.0%')
        self.temporal_position_label.setObjectName('coordinateValue')
        coordinate_header.addWidget(self.temporal_position_label)
        coordinate_header.addStretch()
        self.connection_label = QLabel('WARCRAFT OFFLINE')
        self.connection_label.setObjectName('connectionStandby')
        coordinate_header.addWidget(self.connection_label)
        layout.addLayout(coordinate_header)
        time_bar = QHBoxLayout()
        time_bar.setSpacing(8)
        self.time_input = QLineEdit('00:00')
        self.time_input.setPlaceholderText('34:18')
        self.time_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.time_input.setFixedWidth(104)
        self.time_input.setToolTip('Формат: 34:18 или 1:02:03')
        self.timeline = TimelineSlider()
        self.end_label = QLabel('00:00')
        self.end_label.setObjectName('timeLabel')
        time_bar.addWidget(self.time_input)
        time_bar.addWidget(self.timeline, 1)
        time_bar.addWidget(self.end_label)
        layout.addLayout(time_bar)
        seeker_bar = QHBoxLayout()
        seeker_bar.setSpacing(7)
        self.attach_button = QPushButton('Подключить Seeker')
        self.attach_button.setVisible(False)
        self.seek_button = QPushButton('Перейти к таймингу')
        self.seek_button.setProperty('role', 'primary')
        self.cancel_button = QPushButton('Стоп')
        self.cancel_button.setProperty('role', 'ghost')
        self.seek_profile = QComboBox()
        for profile in SEEK_PROFILES.values():
            self.seek_profile.addItem(profile.label, profile.key)
        stored_profile = str(self.settings.value('seek_profile', 'balanced'))
        profile_index = self.seek_profile.findData(stored_profile)
        self.seek_profile.setCurrentIndex(profile_index if profile_index >= 0 else 1)
        self.seek_profile.setToolTip('Eco работает бережнее, Balanced — до 32x, Maximum — максимально быстро.')
        self.seek_profile.currentIndexChanged.connect(lambda: self.settings.setValue('seek_profile', self.seek_profile.currentData()))
        self.seek_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        seeker_bar.addWidget(self.attach_button)
        seeker_bar.addWidget(self.seek_button)
        seeker_bar.addWidget(self.cancel_button)
        seeker_bar.addWidget(self.seek_profile)
        seeker_bar.addStretch()
        layout.addLayout(seeker_bar)
        self.seek_status = QLabel('Выбери событие или введи точный тайминг.')
        self.seek_status.setObjectName('hint')
        layout.addWidget(self.seek_status)
        self.seek_metrics_label = QLabel('Seeker подключается автоматически после запуска реплея.')
        self.seek_metrics_label.setObjectName('hint')
        layout.addWidget(self.seek_metrics_label)
        self.attach_button.clicked.connect(lambda: self.seeker.attach_to_warcraft())
        self.seek_button.clicked.connect(self.seek_to_target)
        self.cancel_button.clicked.connect(self.seeker.cancel)
        self.timeline.valueChanged.connect(self._temporal_position_changed)
        self.time_input.returnPressed.connect(self._time_input_submitted)
        return transport

    def _temporal_position_changed(self, value: int) -> None:
        self.time_input.setText(format_time(value))
        duration = max(self.timeline.maximum(), 1)
        progress = min(max(value / duration * 100.0, 0.0), 100.0)
        self.temporal_position_label.setText(f'T+{format_time(value, millis=True)}  /  {progress:05.1f}%')

    def _setup_camera_macro_combo(self, combo: QComboBox, action: str, default_key: int) -> None:
        for label, virtual_key in KEY_CHOICES:
            combo.addItem(label, virtual_key)
        legacy_setting = {'toggle_camera': 'camera_toggle_hotkey', 'reset_view': 'camera_reset_hotkey'}.get(action)
        stored = self.settings.value(f'camera_macro/{action}', self.settings.value(legacy_setting, default_key) if legacy_setting is not None else default_key)
        try:
            key = int(stored)
        except (TypeError, ValueError):
            key = default_key
        index = combo.findData(key)
        if index < 0:
            key = default_key
            index = combo.findData(key)
        combo.setCurrentIndex(max(index, 0))
        self.camera_macro_combos[action] = combo
        self._camera_binding_values[action] = int(combo.currentData())
        combo.currentIndexChanged.connect(lambda _index, selected_action=action: self._camera_binding_changed(selected_action))

    def _camera_binding_changed(self, action: str) -> None:
        combo = self.camera_macro_combos[action]
        new_key = int(combo.currentData())
        old_key = self._camera_binding_values[action]
        conflicting_action = next((candidate for candidate, key in self._camera_binding_values.items() if candidate != action and key == new_key), None)
        if conflicting_action is not None:
            conflicting_combo = self.camera_macro_combos[conflicting_action]
            conflicting_combo.blockSignals(True)
            conflicting_combo.setCurrentIndex(conflicting_combo.findData(old_key))
            conflicting_combo.blockSignals(False)
            self._camera_binding_values[conflicting_action] = old_key
        self._camera_binding_values[action] = new_key
        self._sync_camera_macro_bindings()

    def _sync_camera_macro_bindings(self) -> None:
        bindings = dict(self._camera_binding_values)
        self.camera_input.set_bindings(bindings)
        for action, key in bindings.items():
            self.settings.setValue(f'camera_macro/{action}', key)

    def _normalize_camera_macro_bindings(self) -> None:
        used: set[int] = set()
        defaults = {action: default for action, _, default in CAMERA_MACRO_ACTIONS}
        for action, _, _ in CAMERA_MACRO_ACTIONS:
            key = self._camera_binding_values[action]
            if key in used:
                candidates = (defaults[action], *(candidate for _, candidate in KEY_CHOICES))
                key = next((candidate for candidate in candidates if candidate not in used))
                combo = self.camera_macro_combos[action]
                combo.blockSignals(True)
                combo.setCurrentIndex(combo.findData(key))
                combo.blockSignals(False)
                self._camera_binding_values[action] = key
            used.add(key)

    def _stored_float(self, key: str, default: float, minimum: float, maximum: float) -> float:
        try:
            value = float(self.settings.value(key, default))
        except (TypeError, ValueError):
            value = default
        return min(max(value, minimum), maximum)

    def _camera_motion_settings(self) -> CameraMotionSettings:
        widgets = self.camera_tuning_widgets
        return CameraMotionSettings(move_speed=widgets['move_speed'].value(), rotation_speed=math.radians(widgets['rotation_degrees'].value()), zoom_speed=widgets['zoom_speed'].value(), lift_speed=widgets['lift_speed'].value(), smoothing=widgets['smoothing'].value(), follow_smoothing=widgets['follow_smoothing'].value())

    def _persist_camera_tuning(self, settings: CameraMotionSettings) -> None:
        names = ('move_speed', 'rotation_speed', 'zoom_speed', 'lift_speed', 'smoothing', 'follow_smoothing')
        for name in names:
            self.settings.setValue(f'camera_motion/{name}', getattr(settings, name))

    def _set_camera_tuning_values(self, values: tuple[float, float, float, float, float, float]) -> None:
        self._camera_tuning_sync = True
        try:
            widgets = self.camera_tuning_widgets
            widgets['move_speed'].setValue(values[0])
            widgets['rotation_degrees'].setValue(math.degrees(values[1]))
            widgets['zoom_speed'].setValue(values[2])
            widgets['lift_speed'].setValue(values[3])
            widgets['smoothing'].setValue(values[4])
            widgets['follow_smoothing'].setValue(values[5])
        finally:
            self._camera_tuning_sync = False

    def _camera_preset_changed(self, index: int) -> None:
        values = self.camera_preset.itemData(index)
        self.settings.setValue('camera_preset', index)
        if not isinstance(values, (tuple, list)) or len(values) != 6:
            return
        self._set_camera_tuning_values(tuple((float(value) for value in values)))
        settings = self._camera_motion_settings()
        self._persist_camera_tuning(settings)
        self.camera_service.update_settings(settings)

    def _camera_tuning_changed(self) -> None:
        if self._camera_tuning_sync:
            return
        custom_index = self.camera_preset.count() - 1
        self.camera_preset.blockSignals(True)
        self.camera_preset.setCurrentIndex(custom_index)
        self.camera_preset.blockSignals(False)
        self.settings.setValue('camera_preset', custom_index)
        settings = self._camera_motion_settings()
        self._persist_camera_tuning(settings)
        self.camera_service.update_settings(settings)

    def _drone_settings(self) -> DroneSettings:
        widgets = self.camera_drone_tuning_widgets
        yaw_speed = math.radians(widgets['yaw_degrees'].value())
        return DroneSettings(move_speed=widgets['move_speed'].value(), lift_speed=widgets['lift_speed'].value(), dolly_speed=widgets['dolly_speed'].value(), yaw_speed=yaw_speed, orbit_speed_degrees=widgets['orbit_speed_degrees'].value(), pitch_speed=yaw_speed * 0.72, acceleration_response=widgets['acceleration_response'].value(), braking_response=widgets['braking_response'].value(), follow_response=widgets['follow_response'].value(), bank_angle=math.radians(widgets['bank_degrees'].value()))

    def _persist_drone_settings(self, settings: DroneSettings) -> None:
        values = {'move_speed': settings.move_speed, 'lift_speed': settings.lift_speed, 'dolly_speed': settings.dolly_speed, 'yaw_speed': settings.yaw_speed, 'orbit_speed_degrees': settings.orbit_speed_degrees, 'acceleration_response': settings.acceleration_response, 'braking_response': settings.braking_response, 'follow_response': settings.follow_response, 'bank_angle': settings.bank_angle}
        for name, value in values.items():
            self.settings.setValue(f'camera_drone/{name}', value)

    def _drone_tuning_changed(self) -> None:
        settings = self._drone_settings()
        self._persist_drone_settings(settings)
        self.camera_service.update_drone_settings(settings)

    def _set_drone_tuning_values(self, settings: DroneSettings) -> None:
        values = {'move_speed': settings.move_speed, 'lift_speed': settings.lift_speed, 'dolly_speed': settings.dolly_speed, 'yaw_degrees': math.degrees(settings.yaw_speed), 'orbit_speed_degrees': settings.orbit_speed_degrees, 'acceleration_response': settings.acceleration_response, 'braking_response': settings.braking_response, 'follow_response': settings.follow_response, 'bank_degrees': math.degrees(settings.bank_angle)}
        for name, value in values.items():
            spin = self.camera_drone_tuning_widgets[name]
            spin.blockSignals(True)
            try:
                spin.setValue(value)
            finally:
                spin.blockSignals(False)

    def _restore_camera_preferences(self) -> None:
        answer = QMessageBox.question(self, 'Настройки съёмки', 'Вернуть настройки съёмки по умолчанию?\n\nСбросятся характер камеры, операторские шоты, свой переход, Drone, Orbit и все клавиши управления.\nРеплеи, пути к Warcraft/iCCup и выбранные герои не изменятся.', QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes:
            return
        reset_camera_preferences(self.settings)
        self.camera_preset.blockSignals(True)
        try:
            self.camera_preset.setCurrentIndex(DEFAULT_CAMERA_PRESET_INDEX)
        finally:
            self.camera_preset.blockSignals(False)
        motion_values = CAMERA_MOTION_PRESETS[DEFAULT_CAMERA_PRESET_INDEX][1]
        self._set_camera_tuning_values(motion_values)
        motion_settings = self._camera_motion_settings()
        self.settings.setValue('camera_preset', DEFAULT_CAMERA_PRESET_INDEX)
        self._persist_camera_tuning(motion_settings)
        self.camera_service.update_settings(motion_settings)
        default_drone_settings = DroneSettings()
        self._set_drone_tuning_values(default_drone_settings)
        drone_settings = self._drone_settings()
        self._persist_drone_settings(drone_settings)
        self.camera_service.update_drone_settings(drone_settings)
        defaults = {action: default for action, _, default in CAMERA_MACRO_ACTIONS}
        for action, combo in self.camera_macro_combos.items():
            default_key = defaults[action]
            combo.blockSignals(True)
            try:
                combo.setCurrentIndex(combo.findData(default_key))
            finally:
                combo.blockSignals(False)
            self._camera_binding_values[action] = default_key
        self._sync_camera_macro_bindings()
        self._load_shot_tuning()
        self.settings.sync()
        self.camera_status.setText('Настройки съёмки возвращены по умолчанию')

    def _selected_shot_kind(self) -> CameraTransitionKind:
        value = str(self.camera_shot_editor.currentData())
        return CameraTransitionKind(value)

    def _shot_tuning_values(self, kind: CameraTransitionKind) -> tuple[float, float]:
        base = CAMERA_TRANSITION_PRESETS[kind]
        prefix = f'camera_shot/{kind.value}'
        strength = self._stored_float(f'{prefix}/strength_percent', 100.0, 25.0, 200.0)
        duration = self._stored_float(f'{prefix}/duration_seconds', base.duration_seconds, 0.3, 10.0)
        return (strength, duration)

    def _load_shot_tuning(self) -> None:
        kind = self._selected_shot_kind()
        strength, duration = self._shot_tuning_values(kind)
        self._shot_tuning_sync = True
        try:
            self.camera_shot_strength.setValue(strength)
            self.camera_shot_duration.setValue(duration)
        finally:
            self._shot_tuning_sync = False

    def _shot_tuning_changed(self) -> None:
        if self._shot_tuning_sync:
            return
        kind = self._selected_shot_kind()
        prefix = f'camera_shot/{kind.value}'
        self.settings.setValue(f'{prefix}/strength_percent', self.camera_shot_strength.value())
        self.settings.setValue(f'{prefix}/duration_seconds', self.camera_shot_duration.value())

    def _reset_shot_tuning(self) -> None:
        kind = self._selected_shot_kind()
        prefix = f'camera_shot/{kind.value}'
        self.settings.remove(f'{prefix}/strength_percent')
        self.settings.remove(f'{prefix}/duration_seconds')
        self._load_shot_tuning()

    def _tuned_transition_spec(self, kind: CameraTransitionKind) -> CameraTransitionSpec:
        strength, duration = self._shot_tuning_values(kind)
        return tune_transition(CAMERA_TRANSITION_PRESETS[kind], strength, duration)

    def _custom_transition_spec(self) -> CameraTransitionSpec:

        def bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
            return self._stored_float(f'camera_transition/{name}', default, minimum, maximum)
        track_value = self.settings.value('camera_transition/track_selected', DEFAULT_CUSTOM_TRANSITION.track_selected)
        track_selected = track_value if isinstance(track_value, bool) else str(track_value).casefold() in {'1', 'true', 'yes'}
        return CameraTransitionSpec(distance_delta=bounded_float('distance_delta', DEFAULT_CUSTOM_TRANSITION.distance_delta, -2000.0, 2000.0), pitch_delta=math.radians(bounded_float('pitch_degrees', math.degrees(DEFAULT_CUSTOM_TRANSITION.pitch_delta), -25.0, 25.0)), z_offset_delta=bounded_float('z_offset_delta', DEFAULT_CUSTOM_TRANSITION.z_offset_delta, -500.0, 500.0), duration_seconds=bounded_float('duration_seconds', DEFAULT_CUSTOM_TRANSITION.duration_seconds, 0.3, 10.0), target_response=4.5 if track_selected else 0.0, track_selected=track_selected)

    def _configure_custom_transition(self) -> None:
        current = self._custom_transition_spec()
        dialog = QDialog(self)
        dialog.setWindowTitle('Свой операторский переход')
        dialog.setMinimumWidth(430)
        layout = QVBoxLayout(dialog)
        intro = QLabel('Все параметры проходят одну плавную кривую. Положительная дистанция отдаляет камеру, положительная высота поднимает её.')
        intro.setWordWrap(True)
        intro.setObjectName('hint')
        layout.addWidget(intro)
        form = QFormLayout()
        distance = QDoubleSpinBox()
        distance.setRange(-2000.0, 2000.0)
        distance.setSingleStep(50.0)
        distance.setSuffix(' ед.')
        distance.setValue(current.distance_delta)
        form.addRow('Дистанция', distance)
        height = QDoubleSpinBox()
        height.setRange(-500.0, 500.0)
        height.setSingleStep(25.0)
        height.setSuffix(' ед.')
        height.setValue(current.z_offset_delta)
        form.addRow('Высота', height)
        pitch = QDoubleSpinBox()
        pitch.setRange(-25.0, 25.0)
        pitch.setSingleStep(0.5)
        pitch.setSuffix('°')
        pitch.setValue(math.degrees(current.pitch_delta))
        form.addRow('Наклон', pitch)
        duration = QDoubleSpinBox()
        duration.setRange(0.3, 10.0)
        duration.setSingleStep(0.1)
        duration.setSuffix(' сек.')
        duration.setValue(current.duration_seconds)
        form.addRow('Длительность', duration)
        track_selected = QCheckBox('Удерживать выбранного героя в центре')
        track_selected.setChecked(current.track_selected)
        form.addRow('', track_selected)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.settings.setValue('camera_transition/distance_delta', distance.value())
        self.settings.setValue('camera_transition/z_offset_delta', height.value())
        self.settings.setValue('camera_transition/pitch_degrees', pitch.value())
        self.settings.setValue('camera_transition/duration_seconds', duration.value())
        self.settings.setValue('camera_transition/track_selected', track_selected.isChecked())
        self.camera_status.setText('Свой переход сохранён · повторное нажатие возвращает камеру')

    def _trigger_camera_transition(self, kind: CameraTransitionKind) -> None:
        custom_spec = self._custom_transition_spec() if kind == CameraTransitionKind.CUSTOM else self._tuned_transition_spec(kind)
        self.camera_service.toggle_transition(kind, custom_spec)

    def _build_camera_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 10, 0, 0)
        panel = QFrame()
        panel.setObjectName('playerDetail')
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(20, 18, 20, 18)
        panel_layout.setSpacing(14)
        title = QLabel('ПЛАВНАЯ КАМЕРА')
        title.setObjectName('playerName')
        description = QLabel('Плавное управление камерой прямо во время просмотра реплея.')
        description.setWordWrap(True)
        description.setObjectName('playerMeta')
        panel_layout.addWidget(title)
        panel_layout.addWidget(description)
        controls = QHBoxLayout()
        controls.addWidget(QLabel('Характер движения'))
        self.camera_preset = QComboBox()
        for preset_label, preset_values in CAMERA_MOTION_PRESETS:
            self.camera_preset.addItem(preset_label, preset_values)
        self.camera_preset.addItem('Свои настройки', None)
        stored_preset = int(self.settings.value('camera_preset', DEFAULT_CAMERA_PRESET_INDEX))
        self.camera_preset.setCurrentIndex(min(max(stored_preset, 0), self.camera_preset.count() - 1))
        controls.addWidget(self.camera_preset)
        self.camera_defaults_button = QPushButton('Настройки по умолчанию')
        self.camera_defaults_button.setToolTip('Сбросить камеру, операторские шоты, Drone/Orbit и клавиши управления')
        controls.addWidget(self.camera_defaults_button)
        controls.addStretch()
        self.camera_start_button = QPushButton('Включить плавную камеру')
        self.camera_stop_button = QPushButton('Выключить')
        self.camera_stop_button.setEnabled(False)
        controls.addWidget(self.camera_start_button)
        controls.addWidget(self.camera_stop_button)
        panel_layout.addLayout(controls)
        action_defaults = {action: default for action, _, default in CAMERA_MACRO_ACTIONS}
        self.camera_tool_tabs = QTabWidget()
        self.camera_tool_tabs.setObjectName('cameraToolTabs')
        self.camera_tool_tabs.tabBar().setObjectName('sectionTabBar')
        self.camera_tool_tabs.setMinimumHeight(340)
        panel_layout.addWidget(self.camera_tool_tabs)
        control_page = QWidget()
        control_layout = QVBoxLayout(control_page)
        control_layout.setContentsMargins(16, 14, 16, 14)
        control_layout.setSpacing(12)
        follow_controls = QHBoxLayout()
        self.camera_follow_button = QPushButton('Следовать')
        self.camera_unfollow_button = QPushButton('Отвязать')
        self.camera_smart_follow_button = QPushButton('Smart Follow')
        self.camera_reset_button = QPushButton('Вернуть стандартный обзор')
        self.camera_follow_button.setEnabled(False)
        self.camera_unfollow_button.setEnabled(False)
        self.camera_smart_follow_button.setEnabled(False)
        self.camera_smart_follow_button.setText('Smart Follow')
        self.camera_reset_button.setEnabled(False)
        follow_controls.addWidget(self.camera_follow_button)
        follow_controls.addWidget(self.camera_unfollow_button)
        follow_controls.addWidget(self.camera_smart_follow_button)
        follow_controls.addWidget(self.camera_reset_button)
        follow_controls.addStretch()
        control_layout.addLayout(follow_controls)
        tuning_title = QLabel('ПАРАМЕТРЫ ПОЛЁТА')
        tuning_title.setObjectName('cardTitle')
        control_layout.addWidget(tuning_title)
        tuning_frame = QFrame()
        tuning_frame.setObjectName('transitionCard')
        tuning_grid = QGridLayout(tuning_frame)
        tuning_grid.setContentsMargins(12, 10, 12, 10)
        tuning_grid.setHorizontalSpacing(10)
        tuning_grid.setVerticalSpacing(8)
        self._camera_tuning_sync = False
        self.camera_tuning_widgets: dict[str, QDoubleSpinBox] = {}
        tuning_fields = (('move_speed', 'Перемещение', 5.0, 250.0, 5.0, 0, ' ед./с'), ('rotation_degrees', 'Поворот', 5.0, 240.0, 2.5, 1, '°/с'), ('zoom_speed', 'Зум', 200.0, 8000.0, 100.0, 0, ' ед./с'), ('lift_speed', 'Подъём', 100.0, 8000.0, 100.0, 0, ' ед./с'), ('smoothing', 'Отзывчивость', 0.5, 20.0, 0.2, 1, ''), ('follow_smoothing', 'Плавность Follow', 0.5, 20.0, 0.2, 1, ''))
        for index, (name, label, minimum, maximum, step, decimals, suffix) in enumerate(tuning_fields):
            row, column = divmod(index, 3)
            field_column = column * 2
            tuning_grid.addWidget(QLabel(label), row, field_column)
            spin = QDoubleSpinBox()
            spin.setRange(minimum, maximum)
            spin.setSingleStep(step)
            spin.setDecimals(decimals)
            spin.setSuffix(suffix)
            spin.setKeyboardTracking(False)
            spin.setMinimumWidth(118)
            self.camera_tuning_widgets[name] = spin
            tuning_grid.addWidget(spin, row, field_column + 1)
        control_layout.addWidget(tuning_frame)
        preset_values = self.camera_preset.currentData()
        if not isinstance(preset_values, (tuple, list)):
            preset_values = CAMERA_MOTION_PRESETS[1][1]
        stored_values = (self._stored_float('camera_motion/move_speed', float(preset_values[0]), 5.0, 250.0), self._stored_float('camera_motion/rotation_speed', float(preset_values[1]), math.radians(5.0), math.radians(240.0)), self._stored_float('camera_motion/zoom_speed', float(preset_values[2]), 200.0, 8000.0), self._stored_float('camera_motion/lift_speed', float(preset_values[3]), 100.0, 8000.0), self._stored_float('camera_motion/smoothing', float(preset_values[4]), 0.5, 20.0), self._stored_float('camera_motion/follow_smoothing', float(preset_values[5]), 0.5, 20.0))
        self._set_camera_tuning_values(stored_values)
        self.camera_preset.currentIndexChanged.connect(self._camera_preset_changed)
        for spin in self.camera_tuning_widgets.values():
            spin.valueChanged.connect(lambda _value: self._camera_tuning_changed())
        macro_title = QLabel('ОСНОВНЫЕ БИНДЫ')
        macro_title.setObjectName('cardTitle')
        control_layout.addWidget(macro_title)
        macro_grid = QGridLayout()
        macro_grid.setHorizontalSpacing(12)
        macro_grid.setVerticalSpacing(10)
        for index, (action, label, default_key) in enumerate(CAMERA_CORE_MACRO_ACTIONS):
            row, column = divmod(index, 2)
            label_column = column * 2
            macro_grid.addWidget(QLabel(label), row, label_column)
            bind_combo = QComboBox()
            bind_combo.setMinimumWidth(120)
            self._setup_camera_macro_combo(bind_combo, action, default_key)
            macro_grid.addWidget(bind_combo, row, label_column + 1)
        control_layout.addLayout(macro_grid)
        key_help = QLabel('Стрелки — полёт · Insert/Delete — поворот · Home/End — наклон · Page Up/Page Down — зум · Num 1/Num 0 — высота. Макросы работают прямо в Warcraft без Alt-Tab.')
        key_help.setObjectName('hint')
        key_help.setWordWrap(True)
        control_layout.addWidget(key_help)
        control_layout.addStretch()
        self.camera_tool_tabs.addTab(control_page, 'Управление')
        hero_page = QWidget()
        hero_layout = QVBoxLayout(hero_page)
        hero_layout.setContentsMargins(16, 14, 16, 14)
        hero_title = QLabel('10 целей — полный состав матча для переключения прямо в Warcraft')
        hero_title.setObjectName('playerMeta')
        hero_layout.addWidget(hero_title)
        hero_grid = QGridLayout()
        hero_grid.setHorizontalSpacing(10)
        hero_grid.setVerticalSpacing(8)
        self.camera_hero_slots: list[QComboBox] = []
        for slot_index in range(CAMERA_HERO_SLOT_COUNT):
            action = f'hero_slot_{slot_index + 1}'
            row = slot_index % 5
            column = slot_index // 5 * 3
            hero_grid.addWidget(QLabel(f'Слот {slot_index + 1}'), row, column)
            hero_combo = QComboBox()
            hero_combo.setMinimumWidth(230)
            self.camera_hero_slots.append(hero_combo)
            hero_grid.addWidget(hero_combo, row, column + 1)
            hero_combo.currentIndexChanged.connect(lambda _index, selected_slot=slot_index: self._prepare_camera_hero_slot(selected_slot))
            bind_combo = QComboBox()
            bind_combo.setMinimumWidth(100)
            self._setup_camera_macro_combo(bind_combo, action, action_defaults[action])
            hero_grid.addWidget(bind_combo, row, column + 2)
        self.camera_follow_player = self.camera_hero_slots[0]
        hero_layout.addLayout(hero_grid)
        hero_layout.addStretch()
        self.camera_tool_tabs.addTab(hero_page, 'Герои · 10')
        transition_page = QWidget()
        transition_layout = QVBoxLayout(transition_page)
        transition_layout.setContentsMargins(16, 14, 16, 14)
        transition_intro = QLabel('Готовые операторские движения. Повторное нажатие на тот же шот плавно возвращает исходный кадр.')
        transition_intro.setObjectName('playerMeta')
        transition_intro.setWordWrap(True)
        transition_layout.addWidget(transition_intro)
        shot_tuning = QFrame()
        shot_tuning.setObjectName('transitionCard')
        shot_tuning_layout = QHBoxLayout(shot_tuning)
        shot_tuning_layout.setContentsMargins(12, 10, 12, 10)
        shot_tuning_layout.setSpacing(10)
        shot_tuning_layout.addWidget(QLabel('Настройка шота'))
        self.camera_shot_editor = QComboBox()
        labels_by_kind = {kind: label for _, label, _, kind, _ in CAMERA_TRANSITION_ACTIONS}
        for kind in EDITABLE_CAMERA_TRANSITIONS:
            self.camera_shot_editor.addItem(labels_by_kind[kind], kind.value)
        shot_tuning_layout.addWidget(self.camera_shot_editor)
        shot_tuning_layout.addWidget(QLabel('Сила'))
        self.camera_shot_strength = QDoubleSpinBox()
        self.camera_shot_strength.setRange(25.0, 200.0)
        self.camera_shot_strength.setSingleStep(5.0)
        self.camera_shot_strength.setDecimals(0)
        self.camera_shot_strength.setSuffix('%')
        self.camera_shot_strength.setKeyboardTracking(False)
        shot_tuning_layout.addWidget(self.camera_shot_strength)
        shot_tuning_layout.addWidget(QLabel('Длительность'))
        self.camera_shot_duration = QDoubleSpinBox()
        self.camera_shot_duration.setRange(0.3, 10.0)
        self.camera_shot_duration.setSingleStep(0.1)
        self.camera_shot_duration.setDecimals(1)
        self.camera_shot_duration.setSuffix(' сек.')
        self.camera_shot_duration.setKeyboardTracking(False)
        shot_tuning_layout.addWidget(self.camera_shot_duration)
        reset_shot_button = QPushButton('По умолчанию')
        reset_shot_button.clicked.connect(self._reset_shot_tuning)
        shot_tuning_layout.addWidget(reset_shot_button)
        shot_tuning_layout.addStretch()
        transition_layout.addWidget(shot_tuning)
        self._shot_tuning_sync = False
        self._load_shot_tuning()
        self.camera_shot_editor.currentIndexChanged.connect(lambda _index: self._load_shot_tuning())
        self.camera_shot_strength.valueChanged.connect(lambda _value: self._shot_tuning_changed())
        self.camera_shot_duration.valueChanged.connect(lambda _value: self._shot_tuning_changed())
        transition_grid = QGridLayout()
        transition_grid.setHorizontalSpacing(12)
        transition_grid.setVerticalSpacing(10)
        self.camera_transition_buttons: dict[CameraTransitionKind, QPushButton] = {}
        for index, (action, label, default_key, kind, description_text) in enumerate(CAMERA_TRANSITION_ACTIONS):
            row, column = divmod(index, 2)
            card = QFrame()
            card.setObjectName('transitionCard')
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(12, 10, 12, 10)
            card_layout.setSpacing(7)
            action_line = QHBoxLayout()
            transition_button = QPushButton(label)
            transition_button.setEnabled(False)
            transition_button.clicked.connect(lambda _checked=False, selected_kind=kind: self._trigger_camera_transition(selected_kind))
            self.camera_transition_buttons[kind] = transition_button
            action_line.addWidget(transition_button)
            bind_combo = QComboBox()
            bind_combo.setMinimumWidth(100)
            self._setup_camera_macro_combo(bind_combo, action, default_key)
            action_line.addWidget(bind_combo)
            card_layout.addLayout(action_line)
            shot_description = QLabel(description_text)
            shot_description.setObjectName('hint')
            shot_description.setWordWrap(True)
            card_layout.addWidget(shot_description)
            if kind == CameraTransitionKind.CUSTOM:
                configure_button = QPushButton('Настроить параметры')
                configure_button.clicked.connect(self._configure_custom_transition)
                card_layout.addWidget(configure_button)
            transition_grid.addWidget(card, row, column)
        transition_layout.addLayout(transition_grid)
        transition_layout.addStretch()
        self.camera_tool_tabs.addTab(transition_page, 'Операторские шоты')
        drone_page = QWidget()
        drone_layout = QVBoxLayout(drone_page)
        drone_layout.setContentsMargins(16, 14, 16, 14)
        drone_layout.setSpacing(12)
        drone_intro = QLabel('Свободный полёт с инерцией и автоматическим креном. Захват цели использует выбранного героя: движение вперёд становится наездом, стрейф — ручным облётом, а Orbit ведёт круг автоматически и плавно переходит между тремя радиусами.')
        drone_intro.setObjectName('playerMeta')
        drone_intro.setWordWrap(True)
        drone_layout.addWidget(drone_intro)
        drone_actions = QFrame()
        drone_actions.setObjectName('transitionCard')
        drone_actions_layout = QGridLayout(drone_actions)
        drone_actions_layout.setContentsMargins(12, 10, 12, 10)
        drone_actions_layout.setHorizontalSpacing(10)
        drone_actions_layout.setVerticalSpacing(8)
        self.camera_drone_button = QPushButton('Включить Fly Drone')
        self.camera_drone_lock_button = QPushButton('Захватить героя')
        self.camera_orbit_button = QPushButton('Включить Orbit')
        self.camera_orbit_reverse_button = QPushButton('Сменить направление Orbit')
        self.camera_orbit_in_button = QPushButton('Орбита ближе')
        self.camera_orbit_out_button = QPushButton('Орбита дальше')
        self.camera_orbit_ring_buttons = (self.camera_orbit_in_button, self.camera_orbit_out_button)
        self.camera_drone_turn_left_button = QPushButton('Повернуть влево на 90°')
        self.camera_drone_turn_button = QPushButton('Развернуться на 180°')
        self.camera_drone_turn_right_button = QPushButton('Повернуть вправо на 90°')
        self.camera_drone_turn_buttons = (self.camera_drone_turn_left_button, self.camera_drone_turn_button, self.camera_drone_turn_right_button)
        for action_button in (self.camera_drone_button, self.camera_drone_lock_button, self.camera_orbit_button, self.camera_orbit_reverse_button, *self.camera_orbit_ring_buttons, *self.camera_drone_turn_buttons):
            action_button.setMinimumHeight(28)
        self.camera_drone_button.setEnabled(False)
        self.camera_drone_lock_button.setEnabled(False)
        self.camera_orbit_button.setEnabled(False)
        self.camera_orbit_reverse_button.setEnabled(False)
        for ring_button in self.camera_orbit_ring_buttons:
            ring_button.setEnabled(False)
        for turn_button in self.camera_drone_turn_buttons:
            turn_button.setEnabled(False)
        drone_actions_layout.addWidget(self.camera_drone_button, 0, 0, 1, 2)
        toggle_bind = QComboBox()
        toggle_bind.setMinimumWidth(100)
        self._setup_camera_macro_combo(toggle_bind, 'drone_toggle', action_defaults['drone_toggle'])
        drone_actions_layout.addWidget(toggle_bind, 0, 2)
        drone_actions_layout.addWidget(self.camera_drone_lock_button, 0, 3, 1, 2)
        lock_bind = QComboBox()
        lock_bind.setMinimumWidth(100)
        self._setup_camera_macro_combo(lock_bind, 'drone_target_lock', action_defaults['drone_target_lock'])
        drone_actions_layout.addWidget(lock_bind, 0, 5)
        drone_actions_layout.addWidget(self.camera_drone_turn_left_button, 1, 0)
        turn_left_bind = QComboBox()
        turn_left_bind.setMinimumWidth(100)
        self._setup_camera_macro_combo(turn_left_bind, 'drone_turn_left', action_defaults['drone_turn_left'])
        drone_actions_layout.addWidget(turn_left_bind, 1, 1)
        drone_actions_layout.addWidget(self.camera_drone_turn_button, 1, 2)
        turn_around_bind = QComboBox()
        turn_around_bind.setMinimumWidth(100)
        self._setup_camera_macro_combo(turn_around_bind, 'drone_turn_around', action_defaults['drone_turn_around'])
        drone_actions_layout.addWidget(turn_around_bind, 1, 3)
        drone_actions_layout.addWidget(self.camera_drone_turn_right_button, 1, 4)
        turn_right_bind = QComboBox()
        turn_right_bind.setMinimumWidth(100)
        self._setup_camera_macro_combo(turn_right_bind, 'drone_turn_right', action_defaults['drone_turn_right'])
        drone_actions_layout.addWidget(turn_right_bind, 1, 5)
        drone_actions_layout.addWidget(self.camera_orbit_button, 2, 0, 1, 2)
        orbit_bind = QComboBox()
        orbit_bind.setMinimumWidth(100)
        self._setup_camera_macro_combo(orbit_bind, 'orbit_toggle', action_defaults['orbit_toggle'])
        drone_actions_layout.addWidget(orbit_bind, 2, 2)
        drone_actions_layout.addWidget(self.camera_orbit_reverse_button, 2, 3, 1, 2)
        orbit_reverse_bind = QComboBox()
        orbit_reverse_bind.setMinimumWidth(100)
        self._setup_camera_macro_combo(orbit_reverse_bind, 'orbit_reverse', action_defaults['orbit_reverse'])
        drone_actions_layout.addWidget(orbit_reverse_bind, 2, 5)
        drone_actions_layout.addWidget(self.camera_orbit_in_button, 3, 0, 1, 2)
        orbit_in_bind = QComboBox()
        orbit_in_bind.setMinimumWidth(100)
        self._setup_camera_macro_combo(orbit_in_bind, 'orbit_in', action_defaults['orbit_in'])
        drone_actions_layout.addWidget(orbit_in_bind, 3, 2)
        drone_actions_layout.addWidget(self.camera_orbit_out_button, 3, 3, 1, 2)
        orbit_out_bind = QComboBox()
        orbit_out_bind.setMinimumWidth(100)
        self._setup_camera_macro_combo(orbit_out_bind, 'orbit_out', action_defaults['orbit_out'])
        drone_actions_layout.addWidget(orbit_out_bind, 3, 5)
        drone_actions_layout.addWidget(QLabel('Набрать высоту · удерживать'), 4, 0)
        height_up_bind = QComboBox()
        height_up_bind.setMinimumWidth(100)
        self._setup_camera_macro_combo(height_up_bind, 'drone_height_up', action_defaults['drone_height_up'])
        drone_actions_layout.addWidget(height_up_bind, 4, 1)
        drone_actions_layout.addWidget(QLabel('Сбросить высоту · удерживать'), 4, 2)
        height_down_bind = QComboBox()
        height_down_bind.setMinimumWidth(100)
        self._setup_camera_macro_combo(height_down_bind, 'drone_height_down', action_defaults['drone_height_down'])
        drone_actions_layout.addWidget(height_down_bind, 4, 3)
        drone_actions_layout.setColumnStretch(0, 1)
        drone_actions_layout.setColumnStretch(2, 1)
        drone_actions_layout.setColumnStretch(4, 1)
        drone_layout.addWidget(drone_actions)
        drone_tuning_title = QLabel('ХАРАКТЕР ПОЛЁТА ДРОНА')
        drone_tuning_title.setObjectName('cardTitle')
        drone_layout.addWidget(drone_tuning_title)
        drone_tuning = QFrame()
        drone_tuning.setObjectName('transitionCard')
        drone_grid = QGridLayout(drone_tuning)
        drone_grid.setContentsMargins(12, 10, 12, 10)
        drone_grid.setHorizontalSpacing(10)
        drone_grid.setVerticalSpacing(8)
        defaults = DroneSettings()
        stored_drone_values = {'move_speed': self._stored_float('camera_drone/move_speed', defaults.move_speed, 10.0, 250.0), 'lift_speed': self._stored_float('camera_drone/lift_speed', defaults.lift_speed, 200.0, 5000.0), 'dolly_speed': self._stored_float('camera_drone/dolly_speed', defaults.dolly_speed, 200.0, 5000.0), 'yaw_degrees': math.degrees(self._stored_float('camera_drone/yaw_speed', defaults.yaw_speed, math.radians(10.0), math.radians(180.0))), 'orbit_speed_degrees': self._stored_float('camera_drone/orbit_speed_degrees', defaults.orbit_speed_degrees, 2.0, 90.0), 'acceleration_response': self._stored_float('camera_drone/acceleration_response', defaults.acceleration_response, 0.5, 20.0), 'braking_response': self._stored_float('camera_drone/braking_response', defaults.braking_response, 0.5, 30.0), 'follow_response': self._stored_float('camera_drone/follow_response', defaults.follow_response, 0.5, 20.0), 'bank_degrees': math.degrees(self._stored_float('camera_drone/bank_angle', defaults.bank_angle, 0.0, math.radians(20.0)))}
        drone_fields = (('move_speed', 'Скорость', 10.0, 250.0, 5.0, 0, ' ед./с'), ('lift_speed', 'Подъём', 200.0, 5000.0, 100.0, 0, ' ед./с'), ('dolly_speed', 'Наезд', 200.0, 5000.0, 100.0, 0, ' ед./с'), ('yaw_degrees', 'Поворот', 10.0, 180.0, 2.5, 1, '°/с'), ('orbit_speed_degrees', 'Скорость Orbit', 2.0, 90.0, 1.0, 1, '°/с'), ('acceleration_response', 'Разгон', 0.5, 20.0, 0.2, 1, ''), ('braking_response', 'Торможение', 0.5, 30.0, 0.2, 1, ''), ('follow_response', 'Захват цели', 0.5, 20.0, 0.2, 1, ''), ('bank_degrees', 'Крен', 0.0, 20.0, 0.5, 1, '°'))
        self.camera_drone_tuning_widgets: dict[str, QDoubleSpinBox] = {}
        for index, (name, label, minimum, maximum, step, decimals, suffix) in enumerate(drone_fields):
            row, column = divmod(index, 4)
            field_column = column * 2
            drone_grid.addWidget(QLabel(label), row, field_column)
            spin = QDoubleSpinBox()
            spin.setRange(minimum, maximum)
            spin.setSingleStep(step)
            spin.setDecimals(decimals)
            spin.setSuffix(suffix)
            spin.setKeyboardTracking(False)
            spin.setMinimumWidth(108)
            spin.setValue(stored_drone_values[name])
            self.camera_drone_tuning_widgets[name] = spin
            drone_grid.addWidget(spin, row, field_column + 1)
        for spin in self.camera_drone_tuning_widgets.values():
            spin.valueChanged.connect(lambda _value: self._drone_tuning_changed())
        drone_layout.addWidget(drone_tuning)
        drone_help = QLabel('Стрелки — полёт / орбита · Insert/Delete — поворот · Home/End — наклон · Page Up/Page Down — наезд · Num 7/Num 9 — кольцо ближе/дальше · Num 8/Num 2 — Orbit и реверс · высота и разворот на 180° назначаются выше.')
        drone_help.setObjectName('hint')
        drone_help.setWordWrap(True)
        drone_layout.addWidget(drone_help)
        drone_layout.addStretch()
        drone_page.setMinimumHeight(520)
        drone_scroll = QScrollArea()
        drone_scroll.setWidgetResizable(True)
        drone_scroll.setFrameShape(QFrame.Shape.NoFrame)
        drone_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        drone_scroll.setWidget(drone_page)
        self.camera_tool_tabs.addTab(drone_scroll, 'Fly Drone')
        self._normalize_camera_macro_bindings()
        self._sync_camera_macro_bindings()
        self.camera_status = QLabel('Открой реплей в Warcraft и включи независимый Camera Engine.')
        self.camera_status.setObjectName('connectionOffline')
        panel_layout.addWidget(self.camera_status)
        self.camera_scroll = QScrollArea()
        self.camera_scroll.setObjectName('cameraWorkspaceScroll')
        self.camera_scroll.setWidgetResizable(True)
        self.camera_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.camera_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.camera_scroll.setWidget(panel)
        layout.addWidget(self.camera_scroll, 1)
        self.camera_start_button.clicked.connect(self._start_camera)
        self.camera_stop_button.clicked.connect(self.camera_service.stop)
        self.camera_defaults_button.clicked.connect(self._restore_camera_preferences)
        self.camera_follow_button.clicked.connect(self._follow_camera_player)
        self.camera_unfollow_button.clicked.connect(self.camera_service.clear_follow)
        self.camera_smart_follow_button.clicked.connect(self.camera_service.toggle_smart_follow)
        self.camera_reset_button.clicked.connect(self.camera_service.reset_view)
        self.camera_drone_button.clicked.connect(self.camera_service.toggle_drone)
        self.camera_drone_lock_button.clicked.connect(self.camera_service.toggle_drone_target_lock)
        self.camera_orbit_button.clicked.connect(self.camera_service.toggle_orbit)
        self.camera_orbit_reverse_button.clicked.connect(self.camera_service.reverse_orbit)
        self.camera_orbit_in_button.clicked.connect(lambda: self.camera_service.shift_orbit_ring(-1))
        self.camera_orbit_out_button.clicked.connect(lambda: self.camera_service.shift_orbit_ring(1))
        self.camera_drone_turn_button.clicked.connect(lambda: self.camera_service.turn_drone(DRONE_TURN_DEGREES['drone_turn_around']))
        self.camera_drone_turn_left_button.clicked.connect(lambda: self.camera_service.turn_drone(DRONE_TURN_DEGREES['drone_turn_left']))
        self.camera_drone_turn_right_button.clicked.connect(lambda: self.camera_service.turn_drone(DRONE_TURN_DEGREES['drone_turn_right']))
        return tab

    @staticmethod
    def _configure_table(table: QTableWidget) -> None:
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(34)
        table.horizontalHeader().setHighlightSections(False)

    def _wire_seeker(self) -> None:
        signals = self.seeker.signals
        signals.operation_started.connect(self._seeker_started)
        signals.operation_finished.connect(self._seeker_finished)
        signals.scan_progress.connect(lambda value: self.seek_status.setText(f'Подключаюсь к активному реплею… {value}%'))
        signals.attached.connect(self._seeker_attached)
        signals.seek_progress.connect(self._seek_progress)
        signals.seek_metrics.connect(self._seek_metrics)
        signals.seek_finished.connect(self._seek_done)
        signals.failed.connect(self._seeker_error)
        signals.soft_failed.connect(self._seeker_soft_error)
        signals.seek_replaced.connect(lambda target: self.seek_status.setText(f'Новая точка {format_time(target, millis=True)} принята · останавливаю предыдущую перемотку…'))
        signals.cancelled.connect(lambda: self.seek_status.setText('Перемотка остановлена. Warcraft оставлен на паузе.'))

    def _wire_camera(self) -> None:
        signals = self.camera_service.signals
        signals.operation_started.connect(self._camera_started)
        signals.operation_finished.connect(self._camera_finished)
        signals.ready.connect(self._camera_ready)
        signals.state.connect(self._camera_state_changed)
        signals.stopped.connect(self._camera_stopped)
        signals.following.connect(self._camera_following)
        signals.smart_follow.connect(self._camera_smart_follow)
        signals.hero_slots_ready.connect(self._camera_hero_slots_ready)
        signals.transition.connect(self._camera_transition)
        signals.drone.connect(self._camera_drone)
        signals.drone_target_lock.connect(self._camera_drone_target_lock)
        signals.orbit.connect(self._camera_orbit)
        signals.orbit_ring.connect(self._camera_orbit_ring)
        signals.follow_lost.connect(self._camera_follow_lost)
        signals.failed.connect(self._camera_error)

    def _wire_ability_hud(self) -> None:
        signals = self.ability_hud_service.signals
        signals.operation_started.connect(self._ability_hud_started)
        signals.ready.connect(self._ability_hud_ready)
        signals.snapshot.connect(self._ability_hud_snapshot)
        signals.transient.connect(self._ability_hud_transient)
        signals.failed.connect(self._ability_hud_error)
        signals.stopped.connect(self._ability_hud_stopped)

    def open_file(self) -> None:
        start = str(self.settings.value('last_directory', str(Path.home())))
        filenames, _ = QFileDialog.getOpenFileNames(self, 'Добавить реплеи Warcraft III', start, 'Warcraft replay (*.w3g)')
        if not filenames:
            return
        paths = [Path(filename) for filename in filenames]
        self.settings.setValue('last_directory', str(paths[0].parent))
        for path in paths:
            self._add_replay(path)
        item = self._add_replay(paths[0], select=True)
        self._activate_replay(item)

    def open_folder(self) -> None:
        start = str(self.settings.value('last_directory', str(Path.home())))
        directory = QFileDialog.getExistingDirectory(self, 'Папка с реплеями', start)
        if not directory:
            return
        folder = Path(directory)
        self.settings.setValue('last_directory', str(folder))
        root = folder.resolve(strict=False)
        if root not in self._replay_roots:
            self._replay_roots.append(root)
            self._save_replay_roots()
        replays = discover_replays(root)
        for replay in replays:
            self._add_replay(replay, persist=False)
        if replays:
            item = self._add_replay(replays[0], select=True, persist=False)
            self._activate_replay(item)
        else:
            self.status_label.setText('В выбранной папке нет файлов .w3g.')

    def _save_replay_library(self) -> None:
        paths = sorted((str(path) for path in self._manual_replay_paths if path.is_file()))
        self.settings.setValue('replay_library', paths)

    def _save_replay_roots(self) -> None:
        self.settings.setValue('replay_roots', [str(path) for path in self._replay_roots])

    def _add_replay(self, path: Path, *, select: bool=False, persist: bool=True) -> QListWidgetItem:
        resolved = path.resolve()
        if persist:
            self._manual_replay_paths.add(resolved)
        for index in range(self.replay_list.count()):
            item = self.replay_list.item(index)
            if Path(str(item.data(Qt.ItemDataRole.UserRole))) == resolved:
                if select:
                    self.replay_list.setCurrentItem(item)
                if persist:
                    self._save_replay_library()
                return item
        item = QListWidgetItem(resolved.name)
        item.setSizeHint(QSize(0, 62))
        item.setToolTip(str(resolved))
        item.setData(Qt.ItemDataRole.UserRole, str(resolved))
        self.replay_list.addItem(item)
        self.replay_list.setItemWidget(item, ReplayLibraryCard(resolved))
        count = self.replay_list.count()
        self.library_count_label.setText(f"{count} {('RECORD' if count == 1 else 'RECORDS')}")
        if select:
            self.replay_list.setCurrentItem(item)
        if persist:
            self._save_replay_library()
        return item

    def _activate_replay(self, item: QListWidgetItem) -> None:
        if self._parse_task is not None:
            return
        path = Path(str(item.data(Qt.ItemDataRole.UserRole))).resolve()
        self.launch_replay_button.setEnabled(path.is_file())
        if self.auto_launch_checkbox.isChecked() and self._launch_task is None and (not self.launcher.is_current_replay(path)):
            self._launch_replay_in_warcraft(path)
        if self.report is not None and self.current_path is not None and (path == self.current_path.resolve()):
            return
        cached_report = self._report_cache.get(path)
        if cached_report is not None:
            self.current_path = path
            self._show_report(cached_report)
            return
        self.load_replay(path)

    def _launch_current_replay(self) -> None:
        if self._launch_task is not None:
            return
        item = self.replay_list.currentItem()
        if item is None:
            return
        path = Path(str(item.data(Qt.ItemDataRole.UserRole))).resolve()
        if self._parse_task is None and (self.report is None or self.current_path is None or self.current_path.resolve() != path):
            self.load_replay(path)
        self._launch_replay_in_warcraft(path)

    def _warcraft_executable(self) -> Path | None:
        configured = self.settings.value('warcraft_executable', '')
        manually_configured = str(self.settings.value('launch_paths_manual', 'false')).lower() == 'true'
        if manually_configured and configured:
            executable = Path(str(configured))
            if executable.is_file():
                return executable
        running = self.launcher.running()
        for process in running:
            if process.executable is not None and process.executable.is_file():
                self.settings.setValue('warcraft_executable', str(process.executable))
                return process.executable
        for candidate in likely_warcraft_executables(str(configured or '')):
            if candidate.is_file():
                self.settings.setValue('warcraft_executable', str(candidate))
                return candidate
        filename, _ = QFileDialog.getOpenFileName(self, 'Укажи war3.exe версии 1.26', str(Path('D:\\Warcraft 3')), 'Warcraft III (war3.exe)')
        if not filename:
            return None
        executable = Path(filename)
        self.settings.setValue('warcraft_executable', str(executable))
        return executable

    def _iccup_launcher(self) -> Path | None:
        configured = self.settings.value('iccup_launcher', '')
        if configured:
            candidate = Path(str(configured))
            if candidate.is_file():
                return candidate
        for candidate in self.launcher.running_iccup_launchers():
            self.settings.setValue('iccup_launcher', str(candidate))
            return candidate
        for candidate in likely_iccup_launchers(str(configured or '')):
            if candidate.is_file():
                self.settings.setValue('iccup_launcher', str(candidate))
                return candidate
        return None

    def _configure_launch_paths(self) -> bool:
        dialog = QDialog(self)
        dialog.setWindowTitle('Настройка запуска Warcraft')
        dialog.setMinimumWidth(720)
        layout = QVBoxLayout(dialog)
        description = QLabel('Укажи iCCup Launcher и тот war3.exe версии 1.26a, который запускается через него.')
        description.setWordWrap(True)
        layout.addWidget(description)
        game_row = QHBoxLayout()
        game_label = QLabel('Warcraft:')
        game_label.setFixedWidth(110)
        game_path = QLineEdit(str(self.settings.value('warcraft_executable', '') or ''))
        game_path.setPlaceholderText('D:\\Warcraft 3\\war3.exe')
        game_browse = QPushButton('Обзор…')
        game_row.addWidget(game_label)
        game_row.addWidget(game_path, 1)
        game_row.addWidget(game_browse)
        layout.addLayout(game_row)
        launcher_row = QHBoxLayout()
        launcher_label = QLabel('iCCup Launcher:')
        launcher_label.setFixedWidth(110)
        launcher_path = QLineEdit(str(self.settings.value('iccup_launcher', '') or ''))
        launcher_path.setPlaceholderText('C:\\ICCupGameLauncher\\Launcher\\Launcher.exe')
        launcher_browse = QPushButton('Обзор…')
        launcher_row.addWidget(launcher_label)
        launcher_row.addWidget(launcher_path, 1)
        launcher_row.addWidget(launcher_browse)
        layout.addLayout(launcher_row)

        def browse_game() -> None:
            current = Path(game_path.text().strip().strip('"'))
            start = current.parent if current.is_file() else Path.home()
            filename, _ = QFileDialog.getOpenFileName(dialog, 'Укажи war3.exe версии 1.26a', str(start), 'Warcraft III (war3.exe);;Программы (*.exe)')
            if filename:
                game_path.setText(filename)

        def browse_launcher() -> None:
            current = Path(launcher_path.text().strip().strip('"'))
            start = current.parent if current.is_file() else Path.home()
            filename, _ = QFileDialog.getOpenFileName(dialog, 'Укажи iCCup Launcher.exe', str(start), 'iCCup Launcher (Launcher.exe);;Программы (*.exe)')
            if filename:
                launcher_path.setText(filename)
        game_browse.clicked.connect(browse_game)
        launcher_browse.clicked.connect(browse_launcher)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel_button = QPushButton('Отмена')
        save_button = QPushButton('Сохранить')
        save_button.setDefault(True)
        buttons.addWidget(cancel_button)
        buttons.addWidget(save_button)
        layout.addLayout(buttons)
        cancel_button.clicked.connect(dialog.reject)

        def save_paths() -> None:
            game = Path(game_path.text().strip().strip('"'))
            launcher = Path(launcher_path.text().strip().strip('"'))
            if not game.is_file():
                QMessageBox.warning(dialog, 'Настройка запуска', 'Не найден указанный war3.exe.')
                return
            if not launcher.is_file():
                QMessageBox.warning(dialog, 'Настройка запуска', 'Не найден указанный iCCup Launcher.exe.')
                return
            self.settings.setValue('warcraft_executable', str(game.resolve()))
            self.settings.setValue('iccup_launcher', str(launcher.resolve()))
            self.settings.setValue('launch_paths_manual', True)
            dialog.accept()
        save_button.clicked.connect(save_paths)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        self.status_label.setText('Пути Warcraft и iCCup Launcher сохранены.')
        return True

    def _is_iccup_replay(self, path: Path) -> bool:
        report = self._report_cache.get(path.resolve())
        if report is None and self.current_path == path.resolve():
            report = self.report
        return bool(report is not None and 'iccup' in report.map_path.lower() or 'iccup' in path.name.lower())

    def _launch_replay_in_warcraft(self, path: Path, *, backward_seek: bool=False) -> bool:
        if self._launch_task is not None:
            self.seek_status.setText('Другой replay уже запускается; дождись подтверждения.')
            return False
        if backward_seek:
            self._backward_launch_armed = True
        else:
            self._clear_backward_seek()
            self._last_requested_replay_time = None
        try:
            executable = self._warcraft_executable()
            if executable is None:
                return False
            running = self.launcher.running()
            external = [process for process in running if not self.launcher.owns_process(process.pid)]
            if external:
                answer = QMessageBox.question(self, 'Перезапустить Warcraft?', 'Чтобы открыть выбранный реплей, Warcraft нужно закрыть и запустить снова. Закрыть игру сейчас?', QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes)
                if answer != QMessageBox.StandardButton.Yes:
                    return False
            self.camera_service.stop()
            self.ability_hud_service.stop()
            self.ability_hud_window.set_active(False)
            self.seeker.detach()
            self.attach_button.setVisible(False)
            iccup_launcher = self._iccup_launcher()
            if iccup_launcher is None and self._is_iccup_replay(path):
                answer = QMessageBox.question(self, 'iCCup Launcher не найден', 'Этот реплей создан на iCCup, но ReplayLab не нашёл Launcher.exe. Указать пути к iCCup Launcher и war3.exe?', QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Yes)
                if answer != QMessageBox.StandardButton.Yes:
                    return False
                if not self._configure_launch_paths():
                    return False
                executable = self._warcraft_executable()
                iccup_launcher = self._iccup_launcher()
                if executable is None or iccup_launcher is None:
                    return False
        except (WarcraftLaunchError, SeekBackendError, OSError) as exc:
            QMessageBox.warning(self, 'Запуск Warcraft', str(exc))
            return False
        self.connection_label.setText('Открываю replay через iCCup · ввод в игровых окнах временно защищён…' if iccup_launcher is not None else 'Запускаю Warcraft…')
        self.connection_label.setObjectName('connectionOffline')
        self.connection_label.style().unpolish(self.connection_label)
        self.connection_label.style().polish(self.connection_label)
        self._set_launch_busy(True)
        task = LaunchTask(self.launcher, executable, path, iccup_launcher, replace_running=bool(running))
        task.signals.ready.connect(self._launch_ready)
        task.signals.failed.connect(self._launch_failed)
        task.signals.finished.connect(self._launch_finished)
        self._launch_task = task
        QThreadPool.globalInstance().start(task)
        return True

    def _set_launch_busy(self, busy: bool) -> None:
        current = self.replay_list.currentItem()
        can_launch = not busy and current is not None and Path(str(current.data(Qt.ItemDataRole.UserRole))).is_file()
        self.launch_replay_button.setEnabled(can_launch)
        self.launch_paths_button.setEnabled(not busy)
        self.auto_launch_checkbox.setEnabled(not busy)

    def _launch_ready(self, result: tuple[int, Path, str, bool]) -> None:
        pid, path, launch_mode, launch_verified = result
        LOGGER.info('Replay launch ready: pid=%s path=%s mode=%s verified=%s', pid, path, launch_mode, launch_verified)
        self.connection_label.setText('Replay запущен' if launch_verified else 'Warcraft запускается')
        self.connection_label.setObjectName('connectionOffline')
        self.connection_label.style().unpolish(self.connection_label)
        self.connection_label.style().polish(self.connection_label)
        if launch_verified:
            self.seek_status.setText(f'{path.name} запущен.')
        else:
            self.seek_status.setText(f'{path.name} открыт. Жду загрузки реплея…')
        self._schedule_auto_attach(pid, delay_ms=150 if launch_verified else 2500)
        if post_attach_seek_target(self._backward_launch_armed, self._pending_backward_seek) is not None:
            self.seek_status.setText('Возврат назад · replay перезапущен, подключаюсь…')

    def _launch_failed(self, message: str) -> None:
        LOGGER.error('Replay launch failed: %s', message)
        self._auto_attach_pid = None
        self.attach_button.setVisible(False)
        if self._pending_backward_seek is not None:
            self._clear_backward_seek()
        self.connection_label.setText('Replay не запущен')
        QMessageBox.warning(self, 'Запуск Warcraft', message)

    def _launch_finished(self) -> None:
        self._launch_task = None
        self._set_launch_busy(False)

    def _schedule_auto_attach(self, pid: int, *, delay_ms: int) -> None:
        self._auto_attach_pid = pid
        self._auto_attach_deadline = time.monotonic() + 25.0
        self.attach_button.setVisible(False)
        QTimer.singleShot(delay_ms, self._auto_attach_seeker)

    def _auto_attach_seeker(self) -> None:
        pid = self._auto_attach_pid
        if pid is None or self.seeker.attached:
            return
        if time.monotonic() >= self._auto_attach_deadline:
            self._auto_attach_pid = None
            self.seek_status.setText('Replay запущен, но Seeker не успел подключиться. Перезапусти replay для новой автоматической попытки.')
            self.attach_button.setVisible(False)
            return
        if self.seeker.busy:
            QTimer.singleShot(250, self._auto_attach_seeker)
            return
        self.seek_status.setText('Replay запущен · готовлю Instant Seek…')
        self.seeker.attach_to_warcraft(pid, quiet=True)

    def load_replay(self, path: Path) -> None:
        self.current_path = path.resolve()
        self.report = None
        self._set_table_focus_mode(False)
        self.full_table_button.setEnabled(False)
        try:
            size_mib = path.stat().st_size / (1024.0 * 1024.0)
            source_size = f'{size_mib:.1f} MB'
        except OSError:
            source_size = 'SIZE UNKNOWN'
        self.specimen_name_label.setText(path.stem.upper())
        self.specimen_meta_label.setText(f'W3G  ·  {source_size}  ·  INDEXING TEMPORAL SOURCE')
        self.temporal_context.set_active(False)
        self.temporal_fingerprint.clear()
        self.temporal_source_node.set_value('W3G / INDEXING', 'busy')
        self.temporal_model_node.set_value('BUILDING MODEL', 'busy')
        self._set_system_state('INDEXING W3G', 'busy')
        self.export_button.setEnabled(False)
        self.open_file_button.setEnabled(False)
        self.open_folder_button.setEnabled(False)
        self.replay_list.setEnabled(False)
        self.status_label.setText(f'Разбираю {path.name}…')
        task = ParseTask(path)
        task.signals.ready.connect(self._show_report)
        task.signals.failed.connect(self._parse_error)
        task.signals.finished.connect(self._parse_finished)
        self._parse_task = task
        QThreadPool.globalInstance().start(task)

    def _parse_finished(self) -> None:
        self.open_file_button.setEnabled(True)
        self.open_folder_button.setEnabled(True)
        self.replay_list.setEnabled(True)
        self._parse_task = None

    def _parse_error(self, message: str) -> None:
        LOGGER.error('Replay parsing failed: %s', message)
        forget_failed_replay(self.settings, self.current_path)
        self.temporal_context.set_active(False)
        self.temporal_fingerprint.clear()
        self.full_table_button.setEnabled(False)
        self.temporal_source_node.set_value('SOURCE FAULT', 'error')
        self.temporal_model_node.set_value('NO MODEL', 'error')
        self._set_system_state('PARSER FAULT', 'error')
        self.status_label.setText('Не удалось разобрать реплей.')
        QMessageBox.critical(self, 'Ошибка парсера', message)

    def _show_report(self, report: ReplayReport) -> None:
        self.report = report
        report_path = Path(report.source_file).resolve()
        self.current_path = report_path
        self._report_cache[report_path] = report
        self.settings.setValue('last_replay', report.source_file)
        self.export_button.setEnabled(True)
        game_duration = max(report.parsed_timeline_ms - (report.game_start_ms or 0), 0)
        self.map_card.set_value(Path(report.map_path).name or '—')
        self.duration_card.set_value(format_time(game_duration))
        self.kills_card.set_value(str(len(report.kills)))
        triples = sum((event.count >= 3 for event in report.multi_kills))
        self.moments_card.set_value(f'{len(report.multi_kills)} / {triples} крупных')
        self.specimen_name_label.setText(report_path.stem.upper())
        self.specimen_meta_label.setText(f"W3G  ·  {Path(report.map_path).name or 'UNKNOWN MAP'}  ·  {format_time(game_duration)} RECONSTRUCTED SPAN")
        self.temporal_source_node.set_value('W3G / PARSED', 'online')
        self.temporal_model_node.set_value(f'{len(report.dota_players):02d} IDENTITIES', 'online')
        self.table_focus_name.setText(report_path.stem.upper())
        self.table_focus_meta.setText(f'{len(report.dota_players):02d} PLAYERS  ·  {self.stats_table.columnCount():02d} COLUMNS')
        self.full_table_button.setEnabled(True)
        if not self.seeker.attached:
            self.temporal_runtime_node.set_value('OFFLINE', 'idle')
            self.connection_label.setText('WARCRAFT OFFLINE')
            self.connection_label.setObjectName('connectionStandby')
            self.connection_label.style().unpolish(self.connection_label)
            self.connection_label.style().polish(self.connection_label)
        self.temporal_context.set_active(True)
        self._set_system_state('REPLAY READY', 'online')
        self._fill_stats(report)
        self._fill_camera_players(report)
        self._fill_moments(report, game_duration)
        self.temporal_fingerprint.set_events(self._replay_moments, game_duration)
        self._refresh_chat()
        self.seek_button.setEnabled(self.seeker.attached)
        self.status_label.setText(f'{Path(report.source_file).name} · {len(report.dota_players)} игроков · {len(report.kills)} убийств')

    def _fill_stats(self, report: ReplayReport) -> None:
        table = self.stats_table
        table.setSortingEnabled(False)
        table.setRowCount(len(report.dota_players))
        for row, player in enumerate(report.dota_players):
            item_lines: list[str] = []
            for rawcode, name, cost in zip(player.final_item_rawcodes, player.final_item_names, player.final_item_costs):
                if not rawcode:
                    continue
                label = name or rawcode
                if cost is not None:
                    label += f' — {number(cost)} gold'
                item_lines.append(label)
            if item_lines:
                inventory_tooltip = '\n'.join(item_lines)
            elif player.inventory_source is not None:
                inventory_tooltip = 'Пустой инвентарь'
            else:
                inventory_tooltip = 'Карта не записала инвентарь этого игрока'
            if player.inventory_source == 'game-stats-partial-player':
                inventory_tooltip += f'\nТочный снимок игрока внутри незавершённого блока game_stats на {format_time(player.inventory_game_time_ms)}.'
            elif player.inventory_source == 'game-stats-json':
                inventory_tooltip += f'\nСнимок карты на {format_time(player.inventory_game_time_ms)}.'
            elif player.inventory_source == 'final-table':
                inventory_tooltip += '\nФинальная таблица карты.'
            elif player.inventory_source == 'recorded-item-ledger':
                inventory_tooltip += f'\nСостав восстановлен по записанным событиям появления и удаления предметов на {format_time(player.inventory_game_time_ms)}. Порядок слотов и поздно использованные расходники могут отличаться.'
            result = 'Победа' if player.won is True else 'Поражение' if player.won is False else '—'
            creep_values = [number(player.creep_kills), number(player.creep_denies), number(player.neutral_kills)]
            creep_tooltip: str | None = None
            if player.creep_stats_source in {'periodic-snapshot', 'leave-summary', 'game-stats-partial-player'}:
                creep_values = [f'≈{value}' if value != '—' else value for value in creep_values]
                source_label = 'Статистика игрока при выходе' if player.creep_stats_source == 'leave-summary' else 'Последний доступный срез карты'
                creep_tooltip = f'{source_label} на {format_time(player.creep_stats_game_time_ms)}. Финальный блок статистики не был записан.'
            elif player.creep_stats_source == 'final':
                creep_tooltip = 'Финальная статистика карты.'
            inventory_value = number(player.inventory_value)
            net_worth = number(player.net_worth)
            if player.inventory_source == 'recorded-item-ledger':
                if inventory_value != '—':
                    inventory_value = f'≈{inventory_value}'
                if net_worth != '—':
                    net_worth = f'≈{net_worth}'
            values = [player.name, player.hero_name or player.hero_rawcode or '—', player.side or '—', result, number(player.kills), number(player.deaths), number(player.assists), *creep_values, number(player.final_gold), inventory_value, net_worth, '—' if player.apm_average is None else f'{player.apm_average:.1f}', number(player.apm_peak_60s), format_time(player.apm_peak_game_time_ms), f'{number(player.tower_kills)} / {number(player.rax_kills)}']
            for column, value in enumerate(values):
                tooltip = inventory_tooltip if column in (11, 12) else creep_tooltip if column in (7, 8, 9) else None
                item = table_item(value, tooltip=tooltip)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, player.slot)
                    item.setToolTip(f'Warcraft-слот {player.slot} · network id {player.network_player_id}')
                if column == 1:
                    icon_path = hero_icon_path(player.hero_name)
                    if icon_path is not None:
                        item.setIcon(QIcon(str(icon_path)))
                table.setItem(row, column, item)
            if player.won is True:
                table.item(row, 3).setForeground(QColor('#63d99a'))
            elif player.won is False:
                table.item(row, 3).setForeground(QColor('#ff7a6b'))
        table.setSortingEnabled(True)
        if table.rowCount():
            table.selectRow(0)
            self._stats_selection_changed()

    def _stats_selection_changed(self) -> None:
        if self.report is None:
            return
        row = self.stats_table.currentRow()
        if row < 0:
            return
        player_item = self.stats_table.item(row, 0)
        if player_item is None:
            return
        slot = player_item.data(Qt.ItemDataRole.UserRole)
        player = next((candidate for candidate in self.report.dota_players if candidate.slot == slot), None)
        if player is not None:
            self._show_player_detail(player)
            if hasattr(self, 'camera_follow_player'):
                for index in range(self.camera_follow_player.count()):
                    data = self.camera_follow_player.itemData(index)
                    if isinstance(data, (tuple, list)) and data[0] == slot:
                        self.camera_follow_player.setCurrentIndex(index)
                        break

    def _fill_camera_players(self, report: ReplayReport) -> None:
        if not hasattr(self, 'camera_hero_slots'):
            return
        heroes = replay_hero_targets(report.dota_players)
        for slot_index, combo in enumerate(self.camera_hero_slots):
            combo.clear()
            combo.addItem('—', None)
            for label, data in heroes:
                combo.addItem(label, data)
            combo.setCurrentIndex(slot_index + 1 if slot_index < len(heroes) else 0)
        if hasattr(self, 'ability_hud_player'):
            self.ability_hud_player.blockSignals(True)
            self.ability_hud_player.clear()
            for label, data in heroes:
                self.ability_hud_player.addItem(label, data)
            self.ability_hud_player.blockSignals(False)
            self.ability_hud_start_button.setEnabled(bool(heroes))
        self.camera_follow_button.setEnabled(self.camera_service.running and bool(heroes))

    def _set_inventory_evidence(self, label: str, evidence: str, tooltip: str) -> None:
        self.inventory_evidence.setText(label)
        self.inventory_evidence.setProperty('evidence', evidence)
        self.inventory_evidence.setToolTip(tooltip)
        self.inventory_evidence.style().unpolish(self.inventory_evidence)
        self.inventory_evidence.style().polish(self.inventory_evidence)

    def _show_player_detail(self, player: DotaPlayer) -> None:
        portrait = scaled_pixmap(hero_icon_path(player.hero_name), 62)
        self.hero_portrait.clear()
        if portrait is not None:
            self.hero_portrait.setPixmap(portrait)
        else:
            self.hero_portrait.setText('?')
        result = 'ПОБЕДА' if player.won is True else 'ПОРАЖЕНИЕ' if player.won is False else 'РЕЗУЛЬТАТ НЕИЗВЕСТЕН'
        self.detail_player_name.setText(player.name)
        network_id = '--' if player.network_player_id is None else f'{player.network_player_id:02d}'
        self.player_identity_badge.setText(f'SLOT {player.slot:02d}  ·  NID {network_id}')
        side_key = (player.side or 'unknown').casefold()
        if side_key not in {'sentinel', 'scourge'}:
            side_key = 'unknown'
        self.player_side_signal.setProperty('side', side_key)
        self.player_side_signal.style().unpolish(self.player_side_signal)
        self.player_side_signal.style().polish(self.player_side_signal)
        self.detail_hero_name.setText(f"{player.hero_name or player.hero_rawcode or 'Неизвестный герой'} · {player.side or '—'} · слот {player.slot} · {result}")
        average_apm = '—' if player.apm_average is None else f'{player.apm_average:.1f}'
        self.detail_summary.setText(f'K/D/A {number(player.kills)}/{number(player.deaths)}/{number(player.assists)}   ·   Net worth {number(player.net_worth)}   ·   APM {average_apm} / пик {number(player.apm_peak_60s)}')
        if player.inventory_source in {'game-stats-json', 'game-stats-partial-player'}:
            self.inventory_title.setText(f'ИНВЕНТАРЬ · СРЕЗ {format_time(player.inventory_game_time_ms)}')
            self._set_inventory_evidence('EXACT · RECOVERED' if player.inventory_source == 'game-stats-partial-player' else 'EXACT · SNAPSHOT', 'exact', 'Точный записанный срез конкретного игрока.')
        elif player.inventory_source == 'final-table':
            self.inventory_title.setText('ФИНАЛЬНЫЙ ИНВЕНТАРЬ')
            self._set_inventory_evidence('EXACT · FINAL', 'exact', 'Точная финальная таблица карты.')
        elif player.inventory_source == 'recorded-item-ledger':
            self.inventory_title.setText(f'СОСТАВ ИНВЕНТАРЯ · ≈ {format_time(player.inventory_game_time_ms)}')
            self._set_inventory_evidence('RECONSTRUCTED', 'reconstructed', 'Состав восстановлен из записанного журнала предметов; порядок слотов неизвестен.')
        else:
            self.inventory_title.setText('ИНВЕНТАРЬ · ДАННЫХ НЕТ')
            self._set_inventory_evidence('NO SIGNAL', 'none', 'Карта не оставила достаточного доказательства.')
        for index, slot_label in enumerate(self.inventory_slots):
            rawcode = player.final_item_rawcodes[index] if index < len(player.final_item_rawcodes) else None
            item_name = player.final_item_names[index] if index < len(player.final_item_names) else None
            cost = player.final_item_costs[index] if index < len(player.final_item_costs) else None
            slot_label.clear()
            icon = scaled_pixmap(item_icon_path(item_name), 44)
            if icon is not None:
                slot_label.setPixmap(icon)
            elif rawcode:
                slot_label.setText(rawcode)
            else:
                slot_label.setText('—')
            tooltip = item_name or rawcode or 'Пустой слот'
            if cost is not None and rawcode:
                tooltip += f'\nСтоимость: {number(cost)} gold'
            if player.inventory_source == 'recorded-item-ledger' and rawcode:
                tooltip += '\nПозиция слота не записана картой.'
            slot_label.setToolTip(tooltip)

    def _fill_moments(self, report: ReplayReport, game_duration: int) -> None:
        self._replay_moments = build_replay_moments(report)
        self.timeline.set_events(self._replay_moments, game_duration)
        self.timeline.setValue(0)
        self.end_label.setText(format_time(game_duration))
        self._refresh_moments()

    def _visible_moments(self) -> list[ReplayMoment]:
        filter_key = str(self.moment_filter.currentData() or 'all')
        if filter_key == 'kills':
            return [moment for moment in self._replay_moments if moment.kind == ReplayMomentKind.KILL]
        if filter_key == 'highlights':
            return [moment for moment in self._replay_moments if moment.kind != ReplayMomentKind.KILL]
        return list(self._replay_moments)

    @staticmethod
    def _moment_color(moment: ReplayMoment) -> QColor:
        if moment.kind == ReplayMomentKind.FIRST_BLOOD:
            return QColor('#f2c94c')
        if moment.severity >= 3:
            return QColor('#ff6b57')
        if moment.kind == ReplayMomentKind.MULTI_KILL:
            return QColor('#55a7ff')
        return QColor('#9aa8bb')

    def _refresh_moments(self, *_: object) -> None:
        if not hasattr(self, 'moments_table'):
            return
        events = self._visible_moments()
        if hasattr(self, 'timeline'):
            self.timeline.set_events(events, self.timeline.maximum())
        table = self.moments_table
        table.setRowCount(len(events))
        for row, event in enumerate(events):
            hero = event.killer_hero_name or event.killer_hero_rawcode
            killer = event.killer_name + (f' · {hero}' if hero else '')
            target_time = event_seek_target(event.game_time_ms, self.seek_preroll.value())
            values = [format_time(event.game_time_ms, millis=True), format_time(target_time, millis=True), event.label, killer, ', '.join(event.victim_names)]
            for column, value in enumerate(values):
                item = table_item(value)
                item.setData(Qt.ItemDataRole.UserRole, event.game_time_ms)
                if column == 2:
                    item.setForeground(self._moment_color(event))
                if column == 3:
                    icon_path = hero_icon_path(event.killer_hero_name)
                    if icon_path is not None:
                        item.setIcon(QIcon(str(icon_path)))
                table.setItem(row, column, item)

    def _seek_preroll_changed(self, seconds: int) -> None:
        self.settings.setValue('seek_preroll_seconds', seconds)
        self._refresh_moments()

    def _select_event_time(self, event_time_ms: int, *, label: str) -> None:
        preroll_seconds = self.seek_preroll.value()
        target_time = event_seek_target(event_time_ms, preroll_seconds)
        self.timeline.setValue(target_time)
        self.time_input.setText(format_time(target_time))
        preroll_note = 'точно к событию' if preroll_seconds == 0 else f'за {preroll_seconds} сек'
        self.seek_status.setText(f'{label}: событие {format_time(event_time_ms, millis=True)} · старт {format_time(target_time, millis=True)} ({preroll_note}).')
        if self.seeker.attached:
            self.seek_to_target()

    def _moment_clicked(self, row: int, column: int) -> None:
        item = self.moments_table.item(row, column)
        if item is None:
            return
        game_time = item.data(Qt.ItemDataRole.UserRole)
        if game_time is None:
            return
        event_label = self.moments_table.item(row, 2).text() if self.moments_table.item(row, 2) is not None else 'Событие'
        self._select_event_time(int(game_time), label=event_label)

    def _refresh_chat(self, *_: object) -> None:
        if self.report is None or not hasattr(self, 'chat_table'):
            return
        report = self.report
        game_start = report.game_start_ms or 0
        player_names = {player.player_id: player.name for player in report.players}
        player_names.update({player.network_player_id: player.name for player in report.dota_players})
        filter_key = str(self.chat_filter.currentData() or 'match')
        query = self.chat_search.text().strip().casefold()
        channel_names = {0: 'Общий', 1: 'Союзники', 2: 'Наблюдатели', 7: 'Система'}
        messages: list[tuple[ChatMessage, int, str, str]] = []
        for message in report.chats:
            relative_time = message.time_ms - game_start
            player_name = player_names.get(message.player_id, f'Player {message.player_id}')
            if filter_key == 'match' and (relative_time < 0 or message.mode not in (0, 1, 2)):
                continue
            if filter_key == 'pregame' and relative_time >= 0:
                continue
            if filter_key == 'all_chat' and message.mode != 0:
                continue
            if filter_key == 'allies' and message.mode != 1:
                continue
            if query and query not in (player_name + '\n' + message.text).casefold():
                continue
            messages.append((message, relative_time, player_name, channel_names.get(message.mode, f'Канал {message.mode}' if message.mode is not None else 'Служебный')))
        self.chat_table.setRowCount(len(messages))
        for row, (message, relative_time, player_name, channel) in enumerate(messages):
            values = [format_relative_time(relative_time), channel, player_name, message.text]
            for column, value in enumerate(values):
                item = table_item(value)
                item.setData(Qt.ItemDataRole.UserRole, max(relative_time, 0))
                self.chat_table.setItem(row, column, item)

    def _chat_message_double_clicked(self, row: int, column: int) -> None:
        item = self.chat_table.item(row, column)
        if item is None:
            return
        game_time = item.data(Qt.ItemDataRole.UserRole)
        if game_time is None:
            return
        self.tabs.setCurrentWidget(self.moments_tab)
        self._select_event_time(int(game_time), label='Сообщение в чате')

    def _apply_time_input(self) -> bool:
        try:
            game_time = parse_time_input(self.time_input.text())
        except ValueError:
            self.seek_status.setText('Формат времени: 34:18 или 1:02:03')
            self.time_input.setFocus()
            self.time_input.selectAll()
            return False
        if game_time > self.timeline.maximum():
            self.seek_status.setText(f'Реплей заканчивается на {format_time(self.timeline.maximum())}')
            self.time_input.setFocus()
            self.time_input.selectAll()
            return False
        self.timeline.setValue(game_time)
        self.time_input.setText(format_time(game_time))
        return True

    def _time_input_submitted(self) -> None:
        if not self._apply_time_input():
            return
        if self.seeker.attached:
            self.seek_to_target()
        else:
            self.seek_status.setText(f'Выбран {format_time(self.timeline.value())}')

    def seek_to_target(self) -> None:
        if self.report is None:
            return
        if not self._apply_time_input():
            return
        replay_time = self.timeline.value() + (self.report.game_start_ms or 0)
        profile_key = str(self.seek_profile.currentData() or 'balanced')
        profile = SEEK_PROFILES.get(profile_key, SEEK_PROFILES['balanced'])
        self._last_requested_replay_time = replay_time
        self.seeker.seek(replay_time, profile)

    def _restart_for_backward_seek(self) -> None:
        target = self._last_requested_replay_time
        if target is None or self.current_path is None:
            return
        profile_key = str(self.seek_profile.currentData() or 'balanced')
        self._pending_backward_profile = SEEK_PROFILES.get(profile_key, SEEK_PROFILES['balanced'])
        self._pending_backward_seek = target
        self._pending_backward_deadline = time.monotonic() + 45.0
        self._pending_attach_attempt = False
        if not self._launch_replay_in_warcraft(self.current_path, backward_seek=True):
            self._clear_backward_seek()
            return
        self.seek_status.setText('Возврат назад · перезапускаю реплей и жду загрузки…')

    def _attach_for_backward_seek(self) -> None:
        if self._pending_backward_seek is None:
            return
        if time.monotonic() >= self._pending_backward_deadline:
            self._clear_backward_seek()
            QMessageBox.warning(self, 'Replay Seeker', 'Warcraft не успел загрузить реплей. Нажми подключение и повтори переход.')
            return
        if self.seeker.busy:
            QTimer.singleShot(500, self._attach_for_backward_seek)
            return
        self._pending_attach_attempt = True
        self.seek_status.setText('Возврат назад · подключаюсь к заново загруженному реплею…')
        self.seeker.attach_to_warcraft()

    def _clear_backward_seek(self) -> None:
        self._pending_backward_seek = None
        self._backward_launch_armed = False
        self._pending_backward_profile = None
        self._pending_backward_deadline = 0.0
        self._pending_attach_attempt = False

    def _ability_hud_target(self, live_target: tuple[int, str, str] | None=None) -> tuple[int, str, str, dict[str, AbilityDefinition], tuple[str, ...]]:
        if self.report is None:
            raise SeekBackendError('Сначала выбери распознанный replay.')
        data = live_target if live_target is not None else self.ability_hud_player.currentData()
        if not isinstance(data, (tuple, list)) or len(data) != 3:
            raise SeekBackendError('В replay не найден герой для Skills HUD.')
        profile = get_ability_profile(self.report.map_path)
        definitions = profile.abilities if profile is not None else get_ability_catalog()
        if profile is None:
            LOGGER.info('Skills HUD is using the universal catalog: map=%s', self.report.map_path)
        player_slot = int(data[0])
        hero_rawcode = str(data[1])
        label = str(data[2])
        preferred = tuple(dict.fromkeys((event.ability_rawcode for event in self.report.skill_learns if event.player_slot == player_slot)))
        return (player_slot, hero_rawcode, label, definitions, preferred)

    def _prepare_ability_hud_target(self, live_target: tuple[int, str, str] | None=None, *, sync_selector: bool=False) -> tuple[int, str]:
        player_slot, hero_rawcode, label, definitions, preferred = self._ability_hud_target(live_target)
        self.ability_hud_window.set_target(label, definitions, preferred)
        self._ability_hud_display_target = (player_slot, hero_rawcode)
        if sync_selector:
            self._sync_ability_hud_player(player_slot)
        return (player_slot, hero_rawcode)

    def _sync_ability_hud_player(self, player_slot: int) -> None:
        for index in range(self.ability_hud_player.count()):
            data = self.ability_hud_player.itemData(index)
            if isinstance(data, (tuple, list)) and len(data) >= 1 and (int(data[0]) == player_slot):
                self.ability_hud_player.blockSignals(True)
                self.ability_hud_player.setCurrentIndex(index)
                self.ability_hud_player.blockSignals(False)
                return

    def _start_ability_hud(self) -> None:
        if not self._camera_input_ready:
            self._ability_hud_error('Глобальная клавиша Skills HUD недоступна. Перезапусти ReplayLab.')
            return
        try:
            player_slot, hero_rawcode = self._prepare_ability_hud_target()
        except (SeekBackendError, ValueError) as exc:
            self._ability_hud_error(str(exc))
            return
        self._ability_hud_requested_target = None
        self._ability_hud_selection.clear()
        self.ability_hud_service.start(player_slot, hero_rawcode, targets=self._ability_hud_roster())

    def _auto_start_ability_hud(self, process_id: int) -> None:
        if self.report is None or self.ability_hud_player.count() <= 0 or self.ability_hud_service.busy:
            return
        try:
            player_slot, hero_rawcode = self._prepare_ability_hud_target()
        except (SeekBackendError, ValueError) as exc:
            self.ability_hud_status.setText(str(exc))
            return
        self.ability_hud_status.setText('Replay запущен · включаю Skills HUD автоматически…')
        self._ability_hud_requested_target = None
        self._ability_hud_selection.clear()
        self.ability_hud_service.start(player_slot, hero_rawcode, process_id=process_id, targets=self._ability_hud_roster())

    def _ability_hud_roster(self) -> list[tuple[int, str]]:
        targets: list[tuple[int, str]] = []
        for index in range(self.ability_hud_player.count()):
            data = self.ability_hud_player.itemData(index)
            if isinstance(data, (tuple, list)) and len(data) >= 2:
                targets.append((int(data[0]), str(data[1])))
        return targets

    def _ability_hud_target_changed(self) -> None:
        if self.report is None or self.ability_hud_player.currentIndex() < 0:
            return
        try:
            player_slot, hero_rawcode = self._prepare_ability_hud_target()
            if self.ability_hud_service.busy:
                self._ability_hud_selection.pin_explicit_target(player_slot)
                self._ability_hud_requested_target = (player_slot, hero_rawcode)
                self.ability_hud_service.set_target(player_slot, hero_rawcode, self._ability_hud_address_cache.get((player_slot, hero_rawcode), 0))
        except (SeekBackendError, ValueError) as exc:
            self.ability_hud_status.setText(str(exc))

    def _ability_hud_follow_selection_changed(self, checked: bool) -> None:
        self.settings.setValue('ability_hud_follow_selection', checked)
        self._ability_hud_requested_target = None
        self._ability_hud_selection.clear()
        if not checked and self.ability_hud_service.busy:
            self._ability_hud_target_changed()

    @Slot()
    def _ability_hud_pointer_selection(self) -> None:
        self._ability_hud_selection.begin_pointer_selection()

    def _ability_hud_snapshot(self, snapshot: object) -> None:
        if not hasattr(snapshot, 'hero_rawcode'):
            return
        selected_target = replay_hero_for_selection(self.report.dota_players, snapshot.selected_player_slot, snapshot.selected_unit_rawcode) if self.report is not None else None
        selection_allowed = self._ability_hud_selection.observe(snapshot.selected_unit_address, snapshot.selected_player_slot, snapshot.selected_unit_rawcode, selectable=selected_target is not None)
        current_key = (snapshot.player_slot, snapshot.hero_rawcode)
        if snapshot.hero_address:
            self._ability_hud_address_cache[current_key] = snapshot.hero_address
        if self._ability_hud_requested_target == current_key:
            self._ability_hud_requested_target = None
        waiting_for_target = self._ability_hud_requested_target is not None and self._ability_hud_requested_target != current_key
        current_target = replay_hero_for_selection(self.report.dota_players, snapshot.player_slot, snapshot.hero_rawcode) if self.report is not None else None
        if not waiting_for_target:
            if current_target is not None and self._ability_hud_display_target != current_key:
                self._prepare_ability_hud_target(current_target, sync_selector=True)
            if self.report is not None and snapshot.hero_rawcode == 'H00U':
                snapshot = replace(snapshot, invoked_spell_rawcodes=invoker_spells_at(self.report.invoker_invokes, snapshot.player_slot, snapshot.game_time_ms))
            self.ability_hud_window.update_snapshot(snapshot)
        if self.report is None or not self.ability_hud_follow_selection.isChecked() or (not selection_allowed):
            return
        if selected_target is None:
            return
        selected_key = (selected_target[0], selected_target[1])
        if snapshot.selected_unit_address:
            self._ability_hud_address_cache[selected_key] = snapshot.selected_unit_address
        if selected_key == current_key:
            self._ability_hud_requested_target = None
            return
        if self._ability_hud_requested_target == selected_key:
            return
        try:
            self.ability_hud_service.set_target(selected_key[0], selected_key[1], snapshot.selected_unit_address)
        except (SeekBackendError, ValueError) as exc:
            self.ability_hud_status.setText(str(exc))
            return
        self._ability_hud_requested_target = selected_key

    def _ability_hud_started(self) -> None:
        self.ability_hud_start_button.setEnabled(False)
        self.ability_hud_stop_button.setEnabled(True)
        self.ability_hud_player.setEnabled(False)
        self.ability_hud_status.setText('Жду загрузки реплея и появления героя…')

    def _ability_hud_ready(self, snapshot: object) -> None:
        if not hasattr(snapshot, 'process_id'):
            return
        self.camera_input.set_hud_process(int(snapshot.process_id))
        self.ability_hud_player.setEnabled(True)
        self.ability_hud_stop_button.setEnabled(True)
        self._ability_hud_snapshot(snapshot)
        self.ability_hud_window.set_active(True)
        self.ability_hud_status.setText('Skills HUD активен · F4 показать/скрыть')
        self.ability_hud_status.setObjectName('connectionOnline')
        self.ability_hud_status.style().unpolish(self.ability_hud_status)
        self.ability_hud_status.style().polish(self.ability_hud_status)

    def _ability_hud_transient(self, _message: str) -> None:
        self.ability_hud_status.setText('Жду появления героя в реплее…')

    def _ability_hud_stopped(self) -> None:
        if not hasattr(self, 'ability_hud_start_button'):
            return
        self.ability_hud_window.set_active(False)
        self.camera_input.set_hud_process(None)
        self._ability_hud_display_target = None
        self._ability_hud_requested_target = None
        self._ability_hud_selection.clear()
        self.ability_hud_start_button.setEnabled(self.report is not None and self.ability_hud_player.count() > 0)
        self.ability_hud_stop_button.setEnabled(False)
        self.ability_hud_player.setEnabled(True)
        self.ability_hud_status.setText('Skills HUD выключен')
        self.ability_hud_status.setObjectName('connectionOffline')
        self.ability_hud_status.style().unpolish(self.ability_hud_status)
        self.ability_hud_status.style().polish(self.ability_hud_status)

    def _ability_hud_error(self, message: str) -> None:
        LOGGER.error('Skills HUD stopped: %s', message)
        self.ability_hud_window.set_active(False)
        self.ability_hud_status.setText('Skills HUD недоступен. Подробности сохранены в журнале.' if is_critical_runtime_error(message) else message)
        if is_critical_runtime_error(message):
            QMessageBox.critical(self, 'Skills HUD', 'Skills HUD не может продолжить работу. Переустанови ReplayLab или пришли диагностический журнал.')

    def _seek_after_backward_attach(self, target: int, profile: SeekProfile) -> None:
        if self.seeker.busy:
            QTimer.singleShot(100, lambda: self._seek_after_backward_attach(target, profile))
            return
        self.seeker.seek(target, profile)

    def _start_camera(self) -> None:
        if not self._camera_input_ready:
            self._camera_error('Camera Macro Engine недоступен. Перезапусти ReplayLab.')
            return
        settings = self._camera_motion_settings()
        self._persist_camera_tuning(settings)
        drone_settings = self._drone_settings()
        self._persist_drone_settings(drone_settings)
        self.camera_status.setText('Подключаю камеру…')
        self.camera_service.start(settings, drone_settings)

    def _follow_camera_player(self) -> None:
        self._follow_camera_slot(0)

    def _follow_camera_slot(self, slot_index: int) -> None:
        if not 0 <= slot_index < len(self.camera_hero_slots):
            return
        data = self.camera_hero_slots[slot_index].currentData()
        if not isinstance(data, (tuple, list)) or len(data) != 3:
            self.camera_status.setText('Сначала выбери реплей с распознанными героями.')
            return
        self.camera_service.follow_player_hero(int(data[0]), str(data[1]), str(data[2]))

    def _follow_ability_hud_slot(self, slot_index: int) -> None:
        if not self.ability_hud_service.busy or not 0 <= slot_index < len(self.camera_hero_slots):
            return
        data = self.camera_hero_slots[slot_index].currentData()
        if not isinstance(data, (tuple, list)) or len(data) != 3:
            return
        try:
            player_slot, hero_rawcode = self._prepare_ability_hud_target((int(data[0]), str(data[1]), str(data[2])), sync_selector=True)
            self._ability_hud_selection.pin_explicit_target(player_slot)
            self.ability_hud_service.set_target(player_slot, hero_rawcode, self._ability_hud_address_cache.get((player_slot, hero_rawcode), 0))
        except (SeekBackendError, ValueError) as exc:
            self.ability_hud_status.setText(str(exc))
            return
        self._ability_hud_requested_target = (player_slot, hero_rawcode)

    def _prepare_camera_hero_slot(self, slot_index: int) -> None:
        if not self.camera_service.running or not 0 <= slot_index < len(self.camera_hero_slots):
            return
        data = self.camera_hero_slots[slot_index].currentData()
        if isinstance(data, (tuple, list)) and len(data) == 3:
            self.camera_service.prepare_hero_slots([(int(data[0]), str(data[1]))])

    def _camera_macro_triggered(self, action: str) -> None:
        if action == 'ability_hud_toggle':
            if self.ability_hud_service.running:
                self.ability_hud_window.set_active(not self.ability_hud_window.active)
            elif not self.ability_hud_service.busy:
                self._start_ability_hud()
            return
        if action == 'toggle_camera':
            if self.camera_service.running:
                self.camera_service.stop()
            elif not self.camera_service.busy:
                self._start_camera()
            return
        if action == 'follow_toggle':
            if self.camera_service.following:
                self.camera_service.clear_follow()
            else:
                self._follow_camera_slot(0)
            return
        if action == 'smart_follow_toggle':
            self.camera_service.toggle_smart_follow()
            return
        if action == 'drone_toggle':
            self.camera_service.toggle_drone()
            return
        if action == 'drone_target_lock':
            self.camera_service.toggle_drone_target_lock()
            return
        if action == 'orbit_toggle':
            self.camera_service.toggle_orbit()
            return
        if action == 'orbit_reverse':
            self.camera_service.reverse_orbit()
            return
        if action == 'orbit_in':
            self.camera_service.shift_orbit_ring(-1)
            return
        if action == 'orbit_out':
            self.camera_service.shift_orbit_ring(1)
            return
        drone_turn_degrees = DRONE_TURN_DEGREES.get(action)
        if drone_turn_degrees is not None:
            self.camera_service.turn_drone(drone_turn_degrees)
            return
        if action in {'drone_height_up', 'drone_height_down'}:
            return
        transition_kind = CAMERA_TRANSITION_BY_ACTION.get(action)
        if transition_kind is not None:
            self._trigger_camera_transition(transition_kind)
            return
        if action == 'reset_view':
            self.camera_service.reset_view()
            return
        if action.startswith('hero_slot_'):
            try:
                slot_index = int(action.rsplit('_', 1)[1]) - 1
            except ValueError:
                return
            LOGGER.info('Observer hero bind: slot=%s hud=%s camera=%s', slot_index + 1, self.ability_hud_service.running, self.camera_service.running)
            self._follow_camera_slot(slot_index)
            self._follow_ability_hud_slot(slot_index)

    def _sync_orbit_ring_controls(self, active: bool, ring_index: int) -> str:
        selected = min(max(int(ring_index), 0), 2)
        self.camera_orbit_in_button.setEnabled(active and selected > 0)
        self.camera_orbit_out_button.setEnabled(active and selected < 2)
        return ORBIT_RING_LABELS[selected]

    def _camera_ready(self, state: object) -> None:
        self.camera_start_button.setEnabled(False)
        self.camera_stop_button.setEnabled(True)
        self.camera_preset.setEnabled(True)
        self.camera_follow_button.setEnabled(self.camera_follow_player.currentData() is not None)
        self.camera_unfollow_button.setEnabled(False)
        self.camera_smart_follow_button.setEnabled(False)
        for button in self.camera_transition_buttons.values():
            button.setEnabled(True)
        self.camera_reset_button.setEnabled(True)
        self.camera_drone_button.setEnabled(True)
        self.camera_drone_lock_button.setEnabled(False)
        self.camera_orbit_button.setEnabled(False)
        self.camera_orbit_reverse_button.setEnabled(False)
        self._sync_orbit_ring_controls(False, 1)
        for turn_button in self.camera_drone_turn_buttons:
            turn_button.setEnabled(False)
        self.camera_service.update_settings(self._camera_motion_settings())
        self.camera_service.update_drone_settings(self._drone_settings())
        self.camera_status.setText('Камера активна')
        self.camera_status.setObjectName('connectionOnline')
        self.camera_status.style().unpolish(self.camera_status)
        self.camera_status.style().polish(self.camera_status)
        slots: list[tuple[int, str]] = []
        for combo in self.camera_hero_slots:
            data = combo.currentData()
            if isinstance(data, (tuple, list)) and len(data) == 3:
                slots.append((int(data[0]), str(data[1])))
        self.camera_service.prepare_hero_slots(slots)

    def _camera_state_changed(self, state: object) -> None:
        if self.camera_service.drone_enabled:
            if self.camera_service.orbit_enabled:
                direction = 'влево' if self.camera_service.orbit_direction > 0 else 'вправо'
                ring_label = self._sync_orbit_ring_controls(True, self.camera_service.orbit_ring_index)
                self.camera_status.setText(f'Orbit · {ring_label} орбита · облёт {direction}')
                return
            lock_text = ' · захват цели' if self.camera_service.drone_target_locked else ' · свободный полёт'
            self.camera_status.setText(f'Fly Drone активен{lock_text}')
            return
        if not self.camera_service.following:
            self.camera_status.setText('Камера активна')

    def _camera_stopped(self) -> None:
        if not hasattr(self, 'camera_start_button'):
            return
        self.camera_start_button.setEnabled(self._camera_input_ready)
        self.camera_stop_button.setEnabled(False)
        self.camera_preset.setEnabled(True)
        self.camera_follow_button.setEnabled(False)
        self.camera_unfollow_button.setEnabled(False)
        self.camera_smart_follow_button.setEnabled(False)
        self.camera_smart_follow_button.setText('Smart Follow')
        for button in self.camera_transition_buttons.values():
            button.setEnabled(False)
        self.camera_reset_button.setEnabled(False)
        self.camera_drone_button.setEnabled(False)
        self.camera_drone_button.setText('Включить Fly Drone')
        self.camera_drone_lock_button.setEnabled(False)
        self.camera_drone_lock_button.setText('Захватить героя')
        self.camera_orbit_button.setEnabled(False)
        self.camera_orbit_button.setText('Включить Orbit')
        self.camera_orbit_reverse_button.setEnabled(False)
        self.camera_orbit_reverse_button.setText('Сменить направление Orbit')
        self._sync_orbit_ring_controls(False, 1)
        for turn_button in self.camera_drone_turn_buttons:
            turn_button.setEnabled(False)
        self.camera_status.setText('Плавная камера выключена')
        self.camera_status.setObjectName('connectionOffline')
        self.camera_status.style().unpolish(self.camera_status)
        self.camera_status.style().polish(self.camera_status)

    def _camera_following(self, label: str) -> None:
        self.camera_follow_button.setEnabled(True)
        self.camera_unfollow_button.setEnabled(True)
        self.camera_smart_follow_button.setEnabled(True)
        self.camera_drone_lock_button.setEnabled(self.camera_service.drone_enabled)
        self.camera_orbit_button.setEnabled(True)
        self.camera_orbit_reverse_button.setEnabled(self.camera_service.orbit_enabled)
        self._sync_orbit_ring_controls(self.camera_service.orbit_enabled, self.camera_service.orbit_ring_index)
        if self.camera_service.drone_enabled:
            self.camera_status.setText(f'Fly Drone · цель выбрана: {label} · включи захват')
            return
        self.camera_status.setText(f'Мягко следую за героем · {label}')

    def _camera_smart_follow(self, active: bool) -> None:
        self.camera_smart_follow_button.setText('Smart Follow: вкл' if active else 'Smart Follow')
        if active:
            self.camera_status.setText('Smart Follow активен · камера читает движение героя и смотрит вперёд по траектории')

    def _camera_hero_slots_ready(self, count: int) -> None:
        if self.camera_service.running and (not self.camera_service.following):
            configured = sum((combo.currentData() is not None for combo in self.camera_hero_slots))
            self.camera_status.setText(f'Камера активна · быстрых геройских слотов готово: {count}/{configured}')

    def _camera_transition(self, kind_value: str, subject_label: str, active: bool) -> None:
        label = next((action_label for _, action_label, _, kind, _ in CAMERA_TRANSITION_ACTIONS if kind.value == kind_value), kind_value)
        tracked_subject = subject_label if subject_label != kind_value else ''
        if active:
            subject_suffix = f' · цель {tracked_subject}' if tracked_subject else ''
            self.camera_status.setText(f'{label}{subject_suffix} · повторное нажатие вернёт кадр')
        else:
            self.camera_status.setText(f'{label} · камера плавно возвращается')

    def _camera_drone(self, active: bool) -> None:
        self.camera_drone_button.setText('Выключить Fly Drone' if active else 'Включить Fly Drone')
        self.camera_drone_lock_button.setEnabled(active and self.camera_service.following)
        self.camera_orbit_button.setEnabled(self.camera_service.following)
        self.camera_orbit_reverse_button.setEnabled(active and self.camera_service.orbit_enabled)
        self._sync_orbit_ring_controls(active and self.camera_service.orbit_enabled, self.camera_service.orbit_ring_index)
        for turn_button in self.camera_drone_turn_buttons:
            turn_button.setEnabled(active)
        if not active:
            self.camera_drone_lock_button.setText('Захватить героя')
            if self.camera_service.running:
                self.camera_status.setText('Fly Drone выключен · свободная камера активна')
            return
        self.camera_status.setText('Fly Drone активен · свободный полёт · повторное нажатие выключает режим')

    def _camera_drone_target_lock(self, active: bool) -> None:
        self.camera_drone_lock_button.setText('Отпустить героя' if active else 'Захватить героя')
        if not self.camera_service.drone_enabled:
            self.camera_drone_lock_button.setEnabled(False)
            return
        self.camera_drone_lock_button.setEnabled(self.camera_service.following)
        self.camera_orbit_button.setEnabled(self.camera_service.following)
        self.camera_orbit_reverse_button.setEnabled(active and self.camera_service.orbit_enabled)
        self._sync_orbit_ring_controls(active and self.camera_service.orbit_enabled, self.camera_service.orbit_ring_index)
        self.camera_status.setText('Fly Drone · цель удерживается · стрейф даёт орбитальный облёт' if active else 'Fly Drone · свободный полёт')

    def _camera_orbit(self, active: bool, direction: int) -> None:
        self.camera_orbit_button.setText('Выключить Orbit' if active else 'Включить Orbit')
        self.camera_orbit_button.setEnabled(self.camera_service.running and self.camera_service.following)
        self.camera_orbit_reverse_button.setEnabled(active)
        direction_label = 'влево' if direction > 0 else 'вправо'
        self.camera_orbit_reverse_button.setText(f'Сменить направление · сейчас {direction_label}' if active else 'Сменить направление Orbit')
        ring_label = self._sync_orbit_ring_controls(active, self.camera_service.orbit_ring_index)
        if active:
            self.camera_status.setText(f'Orbit активен · {ring_label} орбита · плавный облёт {direction_label}')

    def _camera_orbit_ring(self, ring_index: int) -> None:
        active = self.camera_service.orbit_enabled
        ring_label = self._sync_orbit_ring_controls(active, ring_index)
        if active:
            self.camera_status.setText(f'Orbit · переход на {ring_label} орбиту · вращение и захват цели продолжаются')

    def _camera_follow_lost(self, message: str) -> None:
        self.camera_unfollow_button.setEnabled(False)
        self.camera_smart_follow_button.setEnabled(False)
        self.camera_smart_follow_button.setText('Smart Follow')
        self.camera_drone_lock_button.setEnabled(False)
        self.camera_drone_lock_button.setText('Захватить героя')
        self.camera_orbit_button.setEnabled(False)
        self.camera_orbit_button.setText('Включить Orbit')
        self.camera_orbit_reverse_button.setEnabled(False)
        self.camera_orbit_reverse_button.setText('Сменить направление Orbit')
        self._sync_orbit_ring_controls(False, 1)
        self.camera_follow_button.setEnabled(self.camera_service.running)
        if self.camera_service.drone_enabled:
            self.camera_status.setText(message or 'Fly Drone · цель отпущена · свободный полёт')
            return
        self.camera_status.setText(message or 'Привязка снята · свободная камера активна')

    def _camera_started(self) -> None:
        self.camera_start_button.setEnabled(False)
        self.camera_stop_button.setEnabled(False)
        self.camera_preset.setEnabled(False)

    def _camera_finished(self) -> None:
        self.camera_preset.setEnabled(True)
        if not self.camera_service.running:
            self.camera_start_button.setEnabled(self._camera_input_ready)

    def _camera_error(self, message: str) -> None:
        friendly = {'war3.exe is not running': 'Warcraft III не запущен. Открой реплей и включи Camera Engine ещё раз.'}.get(message, message)
        LOGGER.error('Camera operation failed: %s', message)
        self.camera_status.setText(friendly)
        if is_critical_runtime_error(message):
            QMessageBox.critical(self, 'Камера', 'Камера не может продолжить работу. Подробности сохранены в диагностическом журнале.')

    def _seeker_started(self, operation: str) -> None:
        self.attach_button.setEnabled(False)
        if operation == 'attach':
            self.attach_button.setVisible(False)
        self.seek_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.seek_profile.setEnabled(False)
        if operation == 'seek':
            self.seek_status.setText('Перематываю…')

    def _seeker_finished(self, operation: str) -> None:
        self.attach_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.seek_profile.setEnabled(True)
        self.seek_button.setEnabled(self.report is not None and self.seeker.attached)
        if operation == 'attach' and self._backward_launch_armed and (self._pending_backward_seek is not None) and (not self.seeker.attached):
            self._pending_attach_attempt = False
            QTimer.singleShot(1500, self._attach_for_backward_seek)

    def _seeker_attached(self, result: AttachResult) -> None:
        self._auto_attach_pid = None
        LOGGER.info('Navigation attached: pid=%s profile=%s match=%s attach_ms=%.1f validation_ms=%.1f scan=%s cache=%s', result.pid, result.build_profile, result.game_dll_match, result.attach_duration_ms, result.binary_validation_ms, result.replay_scan_strategy, result.validation_cache_hit)
        self.connection_label.setText(f'WARCRAFT LIVE · {format_time(result.replay_position_ms)}')
        self.connection_label.setObjectName('connectionOnline')
        self.connection_label.style().unpolish(self.connection_label)
        self.connection_label.style().polish(self.connection_label)
        self.temporal_runtime_node.set_value(f'LIVE · {format_time(result.replay_position_ms)}', 'online')
        self._set_system_state('WARCRAFT LINKED', 'online')
        self.attach_button.setVisible(False)
        self.seek_button.setEnabled(self.report is not None)
        self.seek_status.setText('Навигация по реплею готова')
        self.seek_metrics_label.setText('Можно переходить к любому таймингу.')
        QTimer.singleShot(250, lambda process_id=result.pid: self._auto_start_ability_hud(process_id))
        target = post_attach_seek_target(self._backward_launch_armed, self._pending_backward_seek)
        if target is not None:
            profile = self._pending_backward_profile or SEEK_PROFILES['balanced']
            self._clear_backward_seek()
            self.seek_status.setText('Реплей перезапущен · перематываю к точке назад…')
            QTimer.singleShot(100, lambda: self._seek_after_backward_attach(target, profile))

    def _seek_progress(self, progress: SeekProgress) -> None:
        game_start = self.report.game_start_ms if self.report else 0
        game_position = max(progress.current_replay_time_ms - (game_start or 0), 0)
        self.timeline.setValue(min(game_position, self.timeline.maximum()))
        self.temporal_runtime_node.set_value(f'LIVE · {format_time(game_position)}', 'online')
        details = [f'Сейчас {format_time(game_position, millis=True)}']
        if progress.effective_speed > 0.01:
            details.append(f'{progress.effective_speed:.1f}x')
        if progress.eta_seconds is not None:
            details.append(f'осталось {progress.eta_seconds:.1f} с')
        self.seek_status.setText(' · '.join(details))
        self.seek_metrics_label.setText('Переход выполняется…')

    def _seek_metrics(self, metrics: SeekMetrics) -> None:
        LOGGER.info('Navigation metrics: wall_ms=%.1f command_ms=%.1f first_ms=%s speed=%.2f cpu=%s overshoot_ms=%s profile=%s qos=%s', metrics.wall_duration_ms, metrics.command_latency_ms, metrics.first_advance_ms, metrics.effective_speed, metrics.process_cpu_percent, metrics.overshoot_ms, metrics.profile_key, metrics.high_qos_applied)
        self.seek_metrics_label.setText(f'Переход завершён за {metrics.wall_duration_ms / 1000.0:.2f} с')

    def _seek_done(self, replay_position: int) -> None:
        game_start = self.report.game_start_ms if self.report else 0
        game_position = max(replay_position - (game_start or 0), 0)
        self.timeline.setValue(min(game_position, self.timeline.maximum()))
        self.seek_status.setText(f'{format_time(game_position, millis=True)} · пауза')

    def _seeker_error(self, message: str) -> None:
        LOGGER.error('Replay navigation failed: %s', message)
        backward_message = 'The target is behind the current replay position. Restart/checkpoint support is not connected yet.'
        if message == backward_message:
            self.seek_status.setText('Точка позади · перезапускаю реплей для перехода назад…')
            QTimer.singleShot(100, self._restart_for_backward_seek)
            return
        if self._pending_backward_seek is not None and self._pending_attach_attempt:
            self.seek_status.setText('Реплей ещё загружается · попробую подключиться снова…')
            return
        friendly = message
        replacements = {'war3.exe is not running': 'Warcraft III не запущен. Открой игру, запусти реплей и нажми подключение ещё раз.'}
        friendly = replacements.get(message, friendly)
        self.seek_status.setText(friendly)
        if not self.seeker.attached:
            self.attach_button.setVisible(False)
            self.connection_label.setText('WARCRAFT OFFLINE')
            self.connection_label.setObjectName('connectionOffline')
            self.connection_label.style().unpolish(self.connection_label)
            self.connection_label.style().polish(self.connection_label)
            self.temporal_runtime_node.set_value('LINK FAULT', 'error')
            self._set_system_state('REPLAY READY' if self.report is not None else 'SYSTEM STANDBY', 'online' if self.report is not None else 'idle')
        if is_critical_runtime_error(message):
            QMessageBox.critical(self, 'Навигация по реплею', 'Навигация не может продолжить работу. Подробности сохранены в диагностическом журнале.')

    def _seeker_soft_error(self, message: str) -> None:
        LOGGER.info('Replay navigation is not ready: %s', message)
        if self._auto_attach_pid is not None and time.monotonic() < self._auto_attach_deadline:
            self.seek_status.setText('Warcraft ещё готовит replay · повторяю подключение…')
            if self._pending_backward_seek is None:
                QTimer.singleShot(750, self._auto_attach_seeker)
            return
        self._auto_attach_pid = None
        self.seek_status.setText(f'Instant Seek пока не готов: {message}')
        self.attach_button.setVisible(False)

    def export_json(self) -> None:
        if self.report is None or self.current_path is None:
            return
        suggested = self.current_path.with_suffix('.report.json')
        filename, _ = QFileDialog.getSaveFileName(self, 'Сохранить отчёт', str(suggested), 'JSON (*.json)')
        if not filename:
            return
        try:
            self.report.write_json(filename)
        except OSError as exc:
            QMessageBox.critical(self, 'Ошибка экспорта', str(exc))
            return
        self.status_label.setText(f'Отчёт сохранён: {filename}')

    def closeEvent(self, event: QCloseEvent) -> None:
        self._camera_input_poll.stop()
        self.ability_hud_window.set_active(False)
        self.ability_hud_service.shutdown()
        self.camera_service.shutdown()
        self.camera_input.stop()
        self.seeker.shutdown()
        super().closeEvent(event)

    def _apply_style(self) -> None:
        self.setStyleSheet('\n            QMainWindow, QDialog, QMessageBox { background: #080c12; }\n            QWidget {\n                color: #d8e2ed;\n                font-family: "Segoe UI Variable Text", "Segoe UI";\n                font-size: 10pt;\n            }\n            QWidget#obsidianSurface, QWidget#contentArea {\n                background: transparent;\n            }\n            QFrame#topBar {\n                background: qlineargradient(\n                    x1: 0, y1: 0, x2: 1, y2: 0,\n                    stop: 0 rgba(7, 18, 28, 244),\n                    stop: 0.38 rgba(9, 29, 43, 244),\n                    stop: 1 rgba(7, 15, 23, 244)\n                );\n                border: 1px solid #24465a;\n                border-radius: 14px;\n            }\n            QLabel#labMark {\n                background: #06101a;\n                border: 1px solid #28516a;\n                border-radius: 12px;\n                padding: 1px;\n            }\n            QLabel#appTitle {\n                color: #f4f8fc;\n                font-family: "Segoe UI Variable Display", "Segoe UI";\n                font-size: 16pt;\n                font-weight: 650;\n                letter-spacing: -0.4px;\n            }\n            QLabel#appSubtitle {\n                color: #60768b;\n                font-size: 7pt;\n                font-weight: 700;\n                letter-spacing: 1.5px;\n            }\n            QLabel#labChip {\n                background: rgba(20, 83, 111, 72);\n                border: 1px solid #245267;\n                border-radius: 7px;\n                color: #83cfe8;\n                padding: 5px 9px;\n                font-family: "Cascadia Mono", "Consolas";\n                font-size: 7pt;\n                font-weight: 600;\n            }\n            QLabel#systemState {\n                color: #6f8498;\n                font-family: "Cascadia Mono", "Consolas";\n                font-size: 7pt;\n                font-weight: 700;\n                letter-spacing: 0.8px;\n            }\n            QLabel#systemState[signal="busy"] { color: #72c2f1; }\n            QLabel#systemState[signal="online"] { color: #75dcb1; }\n            QLabel#systemState[signal="error"] { color: #ef8174; }\n            QFrame#temporalContextBar {\n                background: qlineargradient(\n                    x1: 0, y1: 0, x2: 1, y2: 0,\n                    stop: 0 rgba(8, 27, 40, 244),\n                    stop: 0.48 rgba(8, 21, 31, 240),\n                    stop: 1 rgba(7, 18, 27, 244)\n                );\n                border: 1px solid #27546a;\n                border-radius: 12px;\n            }\n            QLabel#specimenEyebrow, QLabel#coordinateTitle,\n            QLabel#diagnosticTitle {\n                color: #5d91aa;\n                font-family: "Cascadia Mono", "Consolas";\n                font-size: 7pt;\n                font-weight: 700;\n                letter-spacing: 1.3px;\n            }\n            QLabel#specimenName {\n                color: #eef8fc;\n                font-family: "Segoe UI Variable Display", "Segoe UI";\n                font-size: 15pt;\n                font-weight: 650;\n                letter-spacing: 0.2px;\n            }\n            QLabel#specimenMeta {\n                color: #587489;\n                font-family: "Cascadia Mono", "Consolas";\n                font-size: 6.8pt;\n                font-weight: 600;\n                letter-spacing: 0.5px;\n            }\n            QLabel#fingerprintTitle {\n                color: #5f9bb5;\n                font-family: "Cascadia Mono", "Consolas";\n                font-size: 6.3pt;\n                font-weight: 700;\n                letter-spacing: 1.1px;\n            }\n            QFrame#temporalStatusNode {\n                background: rgba(11, 27, 39, 210);\n                border: 1px solid #203a4b;\n                border-radius: 9px;\n            }\n            QFrame#temporalStatusNode[signal="busy"] {\n                border-color: #2d6989;\n            }\n            QFrame#temporalStatusNode[signal="online"] {\n                border-color: #285c52;\n            }\n            QFrame#temporalStatusNode[signal="error"] {\n                border-color: #4c3335;\n            }\n            QLabel#temporalNodeTitle {\n                color: #55758a;\n                font-family: "Cascadia Mono", "Consolas";\n                font-size: 6.4pt;\n                font-weight: 700;\n                letter-spacing: 1px;\n            }\n            QLabel#temporalNodeValue {\n                color: #cde0ea;\n                font-family: "Cascadia Mono", "Consolas";\n                font-size: 7.5pt;\n                font-weight: 700;\n            }\n            QLabel#sectionEyebrow, QLabel#cardTitle {\n                color: #6d8398;\n                font-size: 7.5pt;\n                font-weight: 700;\n                letter-spacing: 1.2px;\n            }\n            QLabel#sectionCount {\n                color: #52677b;\n                font-family: "Cascadia Mono", "Consolas";\n                font-size: 7pt;\n                font-weight: 600;\n            }\n            QLabel#sectionTitle {\n                color: #f1f6fb;\n                font-family: "Segoe UI Variable Display", "Segoe UI";\n                font-size: 17pt;\n                font-weight: 650;\n            }\n            QFrame#sidebar, QFrame#statCard, QFrame#playerDetail,\n            QFrame#temporalTransport {\n                background: rgba(16, 24, 34, 238);\n                border: 1px solid #1d2c3a;\n                border-radius: 13px;\n            }\n            QFrame#sidebar {\n                background: rgba(12, 19, 28, 242);\n                border-color: #1a2937;\n            }\n            QFrame#statCard {\n                background: rgba(15, 24, 34, 232);\n                border-color: #203142;\n            }\n            QFrame#temporalTransport {\n                background: rgba(8, 19, 29, 242);\n                border-color: #1d3a4d;\n            }\n            QScrollArea#cameraWorkspaceScroll,\n            QScrollArea#cameraWorkspaceScroll > QWidget > QWidget {\n                background: transparent;\n                border: 0;\n            }\n            QLabel#coordinateValue {\n                color: #8dd8f2;\n                font-family: "Cascadia Mono", "Consolas";\n                font-size: 7.5pt;\n                font-weight: 700;\n                letter-spacing: 0.5px;\n            }\n            QFrame#diagnosticRail {\n                background: rgba(7, 15, 23, 225);\n                border: 1px solid #162938;\n                border-radius: 8px;\n            }\n            QFrame#tableFocusRail {\n                background: qlineargradient(\n                    x1: 0, y1: 0, x2: 1, y2: 0,\n                    stop: 0 rgba(10, 42, 58, 245),\n                    stop: 0.55 rgba(9, 27, 39, 242),\n                    stop: 1 rgba(7, 19, 28, 245)\n                );\n                border: 1px solid #2a6077;\n                border-radius: 11px;\n            }\n            QLabel#focusEyebrow {\n                color: #68b2cf;\n                font-family: "Cascadia Mono", "Consolas";\n                font-size: 6.8pt;\n                font-weight: 700;\n                letter-spacing: 1.2px;\n            }\n            QLabel#focusTitle {\n                color: #f1f8fb;\n                font-family: "Segoe UI Variable Display", "Segoe UI";\n                font-size: 15pt;\n                font-weight: 650;\n            }\n            QLabel#focusMeta {\n                color: #7fb6ca;\n                font-family: "Cascadia Mono", "Consolas";\n                font-size: 7pt;\n                font-weight: 700;\n                letter-spacing: 0.7px;\n            }\n            QLabel#diagnosticMode {\n                color: #456476;\n                font-family: "Cascadia Mono", "Consolas";\n                font-size: 6.7pt;\n                font-weight: 700;\n                letter-spacing: 0.8px;\n            }\n            QFrame#transitionCard {\n                background: rgba(14, 23, 33, 238);\n                border: 1px solid #223448;\n                border-radius: 11px;\n            }\n            QFrame#transitionCard QLabel {\n                background: transparent;\n                border: 0;\n            }\n            QLabel#cardValue {\n                color: #f1f6fb;\n                font-family: "Segoe UI Variable Display", "Segoe UI";\n                font-size: 13.5pt;\n                font-weight: 620;\n            }\n            QLabel#heroPortrait {\n                background: #080e14;\n                border: 1px solid #365774;\n                border-radius: 12px;\n                color: #60758a;\n                font-size: 22pt;\n                font-weight: 700;\n            }\n            QLabel#playerName {\n                color: #f4f7fb;\n                font-family: "Segoe UI Variable Display", "Segoe UI";\n                font-size: 15.5pt;\n                font-weight: 650;\n            }\n            QLabel#identityBadge {\n                background: rgba(25, 55, 73, 170);\n                border: 1px solid #294b60;\n                border-radius: 6px;\n                color: #75b9d5;\n                padding: 3px 7px;\n                font-family: "Cascadia Mono", "Consolas";\n                font-size: 6.5pt;\n                font-weight: 700;\n                letter-spacing: 0.6px;\n            }\n            QFrame#sideSignal {\n                background: #4f6374;\n                border: 0;\n                border-radius: 2px;\n            }\n            QFrame#sideSignal[side="sentinel"] { background: #4ba7ed; }\n            QFrame#sideSignal[side="scourge"] { background: #d06964; }\n            QLabel#playerHero {\n                color: #6eb2ed;\n                font-size: 9.5pt;\n                font-weight: 600;\n            }\n            QLabel#playerMeta { color: #8799aa; }\n            QLabel#itemSlot {\n                background: #080e14;\n                border: 1px solid #2b4054;\n                border-radius: 9px;\n                color: #6f8295;\n                font-family: "Cascadia Mono", "Consolas";\n                font-size: 8pt;\n                font-weight: 700;\n            }\n            QLabel#evidenceBadge {\n                background: rgba(28, 42, 53, 210);\n                border: 1px solid #304657;\n                border-radius: 6px;\n                color: #8094a5;\n                padding: 3px 7px;\n                font-family: "Cascadia Mono", "Consolas";\n                font-size: 6.6pt;\n                font-weight: 700;\n                letter-spacing: 0.5px;\n            }\n            QLabel#evidenceBadge[evidence="exact"] {\n                background: rgba(22, 76, 69, 150);\n                border-color: #347c69;\n                color: #83e2bd;\n            }\n            QLabel#evidenceBadge[evidence="reconstructed"] {\n                background: rgba(78, 61, 24, 150);\n                border-color: #8b7034;\n                color: #e4c774;\n            }\n            QPushButton {\n                background: #152231;\n                border: 1px solid #27394a;\n                border-radius: 8px;\n                padding: 8px 13px;\n                color: #c9d6e2;\n                font-weight: 600;\n            }\n            QPushButton:hover {\n                background: #1b2c3d;\n                border-color: #3b536a;\n                color: #f1f6fb;\n            }\n            QPushButton:pressed {\n                background: #102033;\n                border-color: #4b8ecb;\n            }\n            QPushButton[role="primary"] {\n                background: #2469aa;\n                border-color: #327dbd;\n                color: #ffffff;\n            }\n            QPushButton[role="primary"]:hover {\n                background: #2c78bb;\n                border-color: #55a0df;\n            }\n            QPushButton[role="secondary"] { background: #162433; }\n            QPushButton[density="compact"] {\n                padding: 5px 10px;\n                min-height: 18px;\n                font-size: 8.5pt;\n            }\n            QPushButton[role="ghost"] {\n                background: transparent;\n                border-color: #243546;\n                color: #8da0b3;\n            }\n            QPushButton:disabled {\n                background: #121a24;\n                border-color: #1d2935;\n                color: #526170;\n            }\n            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {\n                background: #090f16;\n                border: 1px solid #2a3c4f;\n                border-radius: 8px;\n                color: #e6edf5;\n                padding: 7px 9px;\n                selection-background-color: #245b8c;\n            }\n            QLineEdit, QSpinBox, QDoubleSpinBox, QLabel#timeLabel {\n                font-family: "Cascadia Mono", "Consolas";\n                font-variant-numeric: tabular-nums;\n            }\n            QLineEdit:focus, QComboBox:focus,\n            QSpinBox:focus, QDoubleSpinBox:focus {\n                border-color: #4b94d3;\n            }\n            QComboBox::drop-down {\n                width: 24px;\n                border: 0;\n                background: transparent;\n            }\n            QComboBox QAbstractItemView {\n                background: #101923;\n                border: 1px solid #2b4156;\n                color: #dce6ef;\n                selection-background-color: #193c5d;\n                outline: 0;\n            }\n            QCheckBox { color: #a7b6c5; spacing: 8px; }\n            QCheckBox::indicator {\n                width: 15px;\n                height: 15px;\n                background: #090f16;\n                border: 1px solid #334a60;\n                border-radius: 4px;\n            }\n            QCheckBox::indicator:checked {\n                background: #327fc2;\n                border-color: #68ace4;\n            }\n            QListWidget, QTableWidget {\n                background: rgba(12, 19, 28, 244);\n                alternate-background-color: rgba(15, 24, 34, 244);\n                border: 1px solid #1d2c3a;\n                border-radius: 11px;\n                gridline-color: transparent;\n                selection-background-color: #173e63;\n                selection-color: #f6f9fc;\n                outline: 0;\n            }\n            QListWidget::item {\n                border-radius: 9px;\n            }\n            QListWidget#replayLibrary {\n                background: transparent;\n                border: 0;\n                padding: 2px 1px;\n                outline: 0;\n            }\n            QListWidget#replayLibrary::item {\n                background: rgba(15, 24, 34, 220);\n                border: 1px solid #1e2d3b;\n                border-radius: 11px;\n                color: transparent;\n            }\n            QListWidget#replayLibrary::item:hover {\n                background: rgba(22, 36, 49, 236);\n                border-color: #354c62;\n            }\n            QListWidget#replayLibrary::item:selected {\n                background: #142f49;\n                border: 1px solid #438bc7;\n            }\n            QLabel#replayCardTitle {\n                color: #dbe5ee;\n                font-size: 9.5pt;\n                font-weight: 620;\n            }\n            QLabel#replayCardMeta {\n                color: #60758a;\n                font-family: "Cascadia Mono", "Consolas";\n                font-size: 6.8pt;\n                font-weight: 600;\n            }\n            QHeaderView::section {\n                background: #121d28;\n                color: #73879a;\n                border: 0;\n                border-bottom: 1px solid #223241;\n                padding: 9px 8px;\n                font-size: 8pt;\n                font-weight: 700;\n            }\n            QTableCornerButton::section {\n                background: #121d28;\n                border: 0;\n                border-bottom: 1px solid #223241;\n            }\n            QTabWidget::pane {\n                border: 1px solid #1d2c3a;\n                border-radius: 11px;\n                background: rgba(10, 16, 24, 238);\n            }\n            QTabBar#productTabBar::tab {\n                background: transparent;\n                color: #72869a;\n                padding: 10px 17px;\n                margin-right: 5px;\n                border: 1px solid transparent;\n                border-radius: 9px;\n                font-weight: 620;\n            }\n            QTabBar#productTabBar::tab:hover {\n                background: #111d28;\n                color: #a9bac9;\n            }\n            QTabBar#productTabBar::tab:selected {\n                background: qlineargradient(\n                    x1: 0, y1: 0, x2: 0, y2: 1,\n                    stop: 0 #1d3950,\n                    stop: 1 #14293b\n                );\n                border-color: #37637f;\n                color: #f0f6fb;\n            }\n            QTabBar#sectionTabBar::tab {\n                background: transparent;\n                color: #718599;\n                padding: 9px 14px;\n                border: 0;\n                border-bottom: 2px solid transparent;\n                font-size: 9pt;\n                font-weight: 600;\n            }\n            QTabBar#sectionTabBar::tab:hover { color: #b4c3d0; }\n            QTabBar#sectionTabBar::tab:selected {\n                color: #dceaf5;\n                border-bottom-color: #4a95d3;\n            }\n            QLabel#hint { color: #74889c; }\n            QLabel#statusLabel {\n                color: #64798d;\n                border: 0;\n                font-family: "Cascadia Mono", "Consolas";\n                font-size: 7.5pt;\n            }\n            QLabel#timeLabel {\n                color: #eaf2f8;\n                font-weight: 650;\n            }\n            QLabel#connectionStandby { color: #71889a; }\n            QLabel#connectionOffline { color: #df786a; }\n            QLabel#connectionOnline { color: #66d49b; }\n            QSlider::groove:horizontal {\n                height: 5px;\n                background: #203746;\n                border-radius: 3px;\n            }\n            QSlider::sub-page:horizontal {\n                background: #4eb8dd;\n                border-radius: 3px;\n            }\n            QSlider::handle:horizontal {\n                background: #e9f3fb;\n                border: 3px solid #42a8d3;\n                width: 12px;\n                margin: -6px 0;\n                border-radius: 9px;\n            }\n            QSplitter::handle { background: transparent; width: 10px; }\n            QScrollBar#floatingScrollBar:vertical {\n                background: transparent;\n                border: 0;\n                width: 10px;\n                margin: 0;\n            }\n            QScrollBar#floatingScrollBar:horizontal {\n                background: transparent;\n                border: 0;\n                height: 10px;\n                margin: 0;\n            }\n            QScrollBar#floatingScrollBar::handle:vertical {\n                background: transparent;\n                min-height: 34px;\n            }\n            QScrollBar#floatingScrollBar::handle:horizontal {\n                background: transparent;\n                min-width: 34px;\n            }\n            QScrollBar#floatingScrollBar::add-line,\n            QScrollBar#floatingScrollBar::sub-line {\n                width: 0;\n                height: 0;\n                background: transparent;\n                border: 0;\n            }\n            QScrollBar#floatingScrollBar::add-page,\n            QScrollBar#floatingScrollBar::sub-page {\n                background: transparent;\n                border: 0;\n            }\n            QAbstractScrollArea::corner {\n                background: transparent;\n                border: 0;\n            }\n            QToolTip {\n                background: #111b25;\n                color: #dbe5ee;\n                border: 1px solid #31475b;\n                padding: 6px;\n            }\n            ')

def main() -> int:
    log_path = configure_diagnostics()
    self_test = '--self-test' in sys.argv
    replay_argument: Path | None = None
    if '--self-test-replay' in sys.argv:
        index = sys.argv.index('--self-test-replay')
        if index + 1 < len(sys.argv):
            replay_argument = Path(sys.argv[index + 1])
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName('ReplayLab')
    if sys.platform == 'win32':
        windows = sys.getwindowsversion()
        LOGGER.info('Windows version detected: %s.%s build %s; log=%s', windows.major, windows.minor, windows.build, log_path)
        if not supported_windows_version(windows.major):
            QMessageBox.critical(None, 'ReplayLab', 'ReplayLab поддерживает Windows 10 и Windows 11.')
            return 27
    icon_path = app_icon_path()
    if icon_path is not None:
        app.setWindowIcon(QIcon(str(icon_path)))
    self_test_settings: QSettings | None = None
    if self_test:
        self_test_settings = QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, 'ReplayLab', 'PackagedSelfTest')
        self_test_settings.clear()
    window = ReplayLabWindow(self_test_settings)
    if self_test:
        try:
            Desktop = window.launcher._prepare_pywinauto()
            Desktop(backend='uia').windows()
        except Exception:
            window.close()
            return 26
        expected_camera_tabs = ('Управление', 'Герои · 10', 'Операторские шоты', 'Fly Drone')
        expected_product_tabs = ('Статистика', 'Просмотр', 'Съёмка')
        expected_statistics_tabs = ('Игроки', 'Чат')
        expected_view_tabs = ('События', 'HUD и оверлеи')
        actual_product_tabs = tuple((window.tabs.tabText(index) for index in range(window.tabs.count())))
        actual_statistics_tabs = tuple((window.stats_sections.tabText(index) for index in range(window.stats_sections.count())))
        actual_camera_tabs = tuple((window.camera_tool_tabs.tabText(index) for index in range(window.camera_tool_tabs.count())))
        actual_view_tabs = tuple((window.view_sections.tabText(index) for index in range(window.view_sections.count())))
        brand_pixmap = window.brand_mark.pixmap()
        if actual_product_tabs != expected_product_tabs or actual_statistics_tabs != expected_statistics_tabs or actual_view_tabs != expected_view_tabs or (actual_camera_tabs != expected_camera_tabs) or (brand_pixmap is None) or brand_pixmap.isNull() or (window.temporal_context.objectName() != 'temporalContextBar') or (window.temporal_transport.objectName() != 'temporalTransport') or ('-test-' in window.lab_chip.text().lower()) or (window.full_table_button.text() != 'Развернуть таблицу') or (window.timeline.minimumHeight() < 54) or (window.tabs.currentWidget() is not window.stats_tab) or (len(window.camera_hero_slots) != CAMERA_HERO_SLOT_COUNT) or (len(window.camera_transition_buttons) != len(CAMERA_TRANSITION_ACTIONS)) or (window.ability_hud_window._cursor_bridge.objectName() != 'abilityHudCursorBridge') or (not window.ability_hud_window._cursor_bridge.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)) or (window.ability_hud_window._cursor_timer.interval() > 16):
            window.close()
            return 23

        def exercise_tabs() -> None:
            for tab_index in range(window.tabs.count()):
                window.tabs.setCurrentIndex(tab_index)
                app.processEvents()
            for tool_index in range(window.camera_tool_tabs.count()):
                window.camera_tool_tabs.setCurrentIndex(tool_index)
                app.processEvents()
            for view_index in range(window.view_sections.count()):
                window.view_sections.setCurrentIndex(view_index)
                app.processEvents()
        window.show()
        app.processEvents()
        exercise_tabs()
        if window.camera_scroll.verticalScrollBar().maximum() <= 0:
            window.close()
            return 23
        if replay_argument is not None:
            if not replay_argument.is_file():
                window.close()
                return 21
            window.load_replay(replay_argument)
            deadline = time.monotonic() + 45.0
            while window._parse_task is not None and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            if window._parse_task is not None or window.report is None:
                window.close()
                return 22
            hero_count = sum((bool(player.hero_rawcode) for player in window.report.dota_players))
            inventory_player = next((player for player in window.report.dota_players if player.inventory_source), None)
            if inventory_player is not None:
                window._show_player_detail(inventory_player)
            if hero_count <= 0 or not window._replay_moments or (not any(window.temporal_fingerprint._bins)) or (window.chat_table.rowCount() <= 0) or (window.system_state_label.text() != 'REPLAY READY') or (window.temporal_source_node.value_label.text() != 'W3G / PARSED') or (not window.temporal_model_node.value_label.text().endswith('IDENTITIES')) or (inventory_player is not None and window.inventory_evidence.text() == 'NO SIGNAL'):
                window.close()
                return 24
            for slot_index, hero_slot in enumerate(window.camera_hero_slots):
                data = hero_slot.currentData()
                if hero_slot.count() != hero_count + 1 or (slot_index < hero_count and (not isinstance(data, (tuple, list)) or len(data) != 3)) or (slot_index >= hero_count and data is not None):
                    window.close()
                    return 25
            window.tabs.setCurrentWidget(window.stats_tab)
            window.stats_sections.setCurrentIndex(0)
            window._set_table_focus_mode(True)
            app.processEvents()
            focus_rows_height = sum((window.stats_table.rowHeight(row) for row in range(window.stats_table.rowCount())))
            if not window._table_focus_mode or not window.table_focus_rail.isVisible() or window.sidebar.isVisible() or window.temporal_context.isVisible() or window.stats_cards.isVisible() or window.player_detail.isVisible() or window.temporal_transport.isVisible() or window.tabs.tabBar().isVisible() or window.stats_sections.tabBar().isVisible() or (window.stats_table.rowCount() != len(window.report.dota_players)) or (window.stats_table.viewport().height() < focus_rows_height):
                window.close()
                return 27
            window._set_table_focus_mode(False)
            app.processEvents()
            if window._table_focus_mode or window.table_focus_rail.isVisible() or (not window.sidebar.isVisible()) or (not window.temporal_context.isVisible()) or (not window.temporal_transport.isVisible()) or (not window.tabs.tabBar().isVisible()):
                window.close()
                return 27
            exercise_tabs()
        window.close()
        app.processEvents()
        return 0
    window.show()
    return app.exec()
if __name__ == '__main__':
    raise SystemExit(main())
