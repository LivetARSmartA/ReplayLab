from __future__ import annotations
import concurrent.futures
import math
import sys
import threading
import time
from pathlib import Path
from typing import Callable
from PySide6.QtCore import QSettings, QSize, QTimer, Qt, QThreadPool, QRunnable, Signal, QObject
from PySide6.QtGui import QColor, QCloseEvent, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem, QLineEdit, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSlider, QSpinBox, QSplitter, QStyle, QStyleOptionSlider, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget
from .assets import app_icon_path, hero_icon_path, item_icon_path, release_build_id
from .camera import CameraMotionSettings, SmoothCameraController
from .camera_input import CameraInputRouter, KEY_CHOICES
from .camera_modes import CAMERA_TRANSITION_PRESETS, DEFAULT_CUSTOM_TRANSITION, CameraTransitionKind, CameraTransitionSpec, tune_transition
from .launcher import WarcraftLaunchError, WarcraftReplayLauncher, likely_iccup_launchers, likely_warcraft_executables
from .moments import ReplayMoment, ReplayMomentKind, build_replay_moments
from .native_camera import DroneSettings
from .parser import ChatMessage, DotaPlayer, ItemTiming, ReplayReport, parse_replay
from .seeker import AttachResult, SEEK_PROFILES, SeekBackendError, SeekCancelled, SeekMetrics, SeekProfile, SeekProgress, Warcraft126MemoryBackend
from .settings import discover_replays, forget_failed_replay, recover_persistent_settings
APP_NAME = 'Warcraft III Replay Lab'
CAMERA_HERO_SLOT_COUNT = 10
CAMERA_CORE_MACRO_ACTIONS = (('toggle_camera', 'Камера: вкл / выкл', 119), ('follow_toggle', 'Follow: вкл / выкл', 118), ('smart_follow_toggle', 'Smart Follow', 116), ('reset_view', 'Вернуть обзор', 120))
CAMERA_DRONE_MACRO_ACTIONS = (('drone_toggle', 'Fly Drone: вкл / выкл', 66), ('drone_target_lock', 'Drone: захват цели', 78), ('orbit_toggle', 'Orbit: вкл / выкл', 104), ('orbit_reverse', 'Orbit: сменить направление', 98), ('orbit_in', 'Orbit: ближнее кольцо', 103), ('orbit_out', 'Orbit: дальнее кольцо', 105), ('drone_turn_left', 'Drone: поворот влево 90°', 100), ('drone_turn_around', 'Drone: разворот 180°', 101), ('drone_turn_right', 'Drone: поворот вправо 90°', 102), ('drone_height_up', 'Drone: набрать высоту', 97), ('drone_height_down', 'Drone: сбросить высоту', 96))
DRONE_TURN_DEGREES = {'drone_turn_left': 90.0, 'drone_turn_around': 180.0, 'drone_turn_right': -90.0}
ORBIT_RING_LABELS = ('ближняя', 'средняя', 'дальняя')
CAMERA_TRANSITION_ACTIONS = (('transition_dolly_out', 'Dolly Out', 121, CameraTransitionKind.DOLLY_OUT, 'Чистый плавный отъезд назад'), ('transition_crane_up', 'Crane Up', 122, CameraTransitionKind.CRANE_UP, 'Вертикальный операторский подъём'), ('transition_reveal', 'Reveal', 117, CameraTransitionKind.REVEAL, 'Подъём, отдаление и наклон с удержанием героя'), ('transition_push_in', 'Push In', 123, CameraTransitionKind.PUSH_IN, 'Мягкий наезд на текущий кадр'), ('transition_focus_pull', 'Focus Pull', 71, CameraTransitionKind.FOCUS_PULL, 'Отдаление с выбранным героем в центре'), ('transition_custom', 'Свой переход', 84, CameraTransitionKind.CUSTOM, 'Своя дистанция, высота, наклон и длительность'))
CAMERA_HERO_MACRO_ACTIONS = (('hero_slot_1', 'Герой 1', 49), ('hero_slot_2', 'Герой 2', 50), ('hero_slot_3', 'Герой 3', 51), ('hero_slot_4', 'Герой 4', 52), ('hero_slot_5', 'Герой 5', 53), ('hero_slot_6', 'Герой 6', 54), ('hero_slot_7', 'Герой 7', 55), ('hero_slot_8', 'Герой 8', 56), ('hero_slot_9', 'Герой 9', 57), ('hero_slot_10', 'Герой 10', 48))
CAMERA_MACRO_ACTIONS = CAMERA_CORE_MACRO_ACTIONS + CAMERA_DRONE_MACRO_ACTIONS + tuple(((action, label, default_key) for action, label, default_key, _, _ in CAMERA_TRANSITION_ACTIONS)) + CAMERA_HERO_MACRO_ACTIONS
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

def number(value: int | None) -> str:
    return '—' if value is None else f'{value:,}'.replace(',', ' ')

def format_seek_speed(value: int) -> str:
    return 'максимум' if value == 65535 else f'{value}x'

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
                self.signals.failed.emit(str(exc))
            except Exception as exc:
                self.signals.failed.emit(f'Неожиданная ошибка Camera Engine: {exc}')
            finally:
                with self._lock:
                    self._busy = False
                self.signals.operation_finished.emit()
        self._executor.submit(guarded)
        return True

    def _runtime_failed(self, message: str) -> None:
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
        self.setMinimumHeight(46)

    def set_events(self, events: list[ReplayMoment], duration_game_ms: int) -> None:
        self._events = list(events)
        self.setMaximum(max(duration_game_ms, 1))
        self.update()

    def paintEvent(self, event: object) -> None:
        super().paintEvent(event)
        if not self._events or self.maximum() <= 0:
            return
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderGroove, self)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for moment in self._events:
            ratio = min(max(moment.game_time_ms / self.maximum(), 0), 1)
            x = groove.left() + round(ratio * groove.width())
            color = QColor('#f2c94c') if moment.kind == ReplayMomentKind.FIRST_BLOOD else QColor('#ff6b57') if moment.severity >= 3 else QColor('#55a7ff') if moment.kind == ReplayMomentKind.MULTI_KILL else QColor('#7f8ea3')
            width = 3 if moment.kind != ReplayMomentKind.KILL else 1
            painter.setPen(QPen(color, width))
            painter.drawLine(x, groove.top() - 8, x, groove.bottom() + 8)

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

class ReplayLabWindow(QMainWindow):

    def __init__(self, settings: QSettings | None=None) -> None:
        super().__init__()
        build_id = release_build_id()
        self.setWindowTitle(f'{APP_NAME} · {build_id}' if build_id else APP_NAME)
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
        self._parse_task: ParseTask | None = None
        self._launch_task: LaunchTask | None = None
        self._last_requested_replay_time: int | None = None
        self._pending_backward_seek: int | None = None
        self._pending_backward_profile: SeekProfile | None = None
        self._pending_backward_deadline = 0.0
        self._pending_attach_attempt = False
        self._auto_attach_pid: int | None = None
        self._auto_attach_deadline = 0.0
        self.seeker = SeekerService()
        self.camera_macro_signals = CameraMacroSignals()
        self.camera_input = CameraInputRouter(self.camera_macro_signals.triggered.emit)
        self.camera_service = CameraService(self.camera_input)
        self.launcher = WarcraftReplayLauncher()
        self._build_ui()
        self._wire_seeker()
        self._wire_camera()
        self._apply_style()
        self.camera_macro_signals.triggered.connect(self._camera_macro_triggered)
        self._camera_input_ready = False
        try:
            self.camera_input.start()
        except SeekBackendError as exc:
            self.camera_start_button.setEnabled(False)
            QTimer.singleShot(0, lambda message=str(exc): self._camera_error(message))
        else:
            self._camera_input_ready = True
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

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)
        toolbar = QHBoxLayout()
        title = QLabel('REPLAY LAB')
        title.setObjectName('appTitle')
        toolbar.addWidget(title)
        toolbar.addStretch()
        self.open_file_button = QPushButton('Добавить реплеи')
        self.open_folder_button = QPushButton('Открыть папку')
        self.export_button = QPushButton('Экспорт JSON')
        self.export_button.setEnabled(False)
        toolbar.addWidget(self.open_file_button)
        toolbar.addWidget(self.open_folder_button)
        if not RELEASE_BUILD:
            toolbar.addWidget(self.export_button)
        root.addLayout(toolbar)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter, 1)
        sidebar = QFrame()
        sidebar.setObjectName('sidebar')
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(12, 12, 12, 12)
        sidebar_layout.addWidget(QLabel('РЕПЛЕИ'))
        self.replay_list = QListWidget()
        self.replay_list.setMinimumWidth(230)
        self.replay_list.setMaximumWidth(360)
        sidebar_layout.addWidget(self.replay_list, 1)
        self.launch_replay_button = QPushButton('Открыть в Warcraft')
        self.launch_paths_button = QPushButton('Настроить запуск')
        self.launch_replay_button.setEnabled(False)
        self.auto_launch_checkbox = QCheckBox('Запускать при выборе')
        self.auto_launch_checkbox.setChecked(str(self.settings.value('auto_launch_replay', 'false')).lower() == 'true')
        self.auto_launch_checkbox.setToolTip('При выборе другого реплея Warcraft будет мягко перезапущен с этим файлом.')
        sidebar_layout.addWidget(self.launch_replay_button)
        sidebar_layout.addWidget(self.launch_paths_button)
        sidebar_layout.addWidget(self.auto_launch_checkbox)
        splitter.addWidget(sidebar)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(10)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_moments_tab(), 'Просмотр')
        self.tabs.addTab(self._build_camera_tab(), 'Съёмка')
        self.tabs.addTab(self._build_stats_tab(), 'Статистика')
        content_layout.addWidget(self.tabs, 1)
        content_layout.addWidget(self._build_transport_bar())
        splitter.addWidget(content)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([270, 1100])
        self.status_label = QLabel('Выбери .w3g — отчёт появится здесь.')
        self.status_label.setObjectName('statusLabel')
        root.addWidget(self.status_label)
        self.setCentralWidget(central)
        self.open_file_button.clicked.connect(self.open_file)
        self.open_folder_button.clicked.connect(self.open_folder)
        self.export_button.clicked.connect(self.export_json)
        self.replay_list.itemClicked.connect(self._activate_replay)
        self.launch_replay_button.clicked.connect(self._launch_current_replay)
        self.launch_paths_button.clicked.connect(self._configure_launch_paths)
        self.auto_launch_checkbox.toggled.connect(lambda checked: self.settings.setValue('auto_launch_replay', checked))

    def _build_stats_tab(self) -> QWidget:
        tab = QWidget()
        outer_layout = QVBoxLayout(tab)
        outer_layout.setContentsMargins(0, 10, 0, 0)
        cards = QHBoxLayout()
        self.map_card = StatCard('КАРТА')
        self.duration_card = StatCard('ИГРОВОЕ ВРЕМЯ')
        self.kills_card = StatCard('УБИЙСТВА')
        self.moments_card = StatCard('СЕРИИ')
        for card in (self.map_card, self.duration_card, self.kills_card, self.moments_card):
            cards.addWidget(card, 1)
        outer_layout.addLayout(cards)
        self.stats_sections = QTabWidget()
        players_page = QWidget()
        layout = QVBoxLayout(players_page)
        layout.setContentsMargins(0, 8, 0, 0)
        detail = QFrame()
        detail.setObjectName('playerDetail')
        detail_layout = QHBoxLayout(detail)
        detail_layout.setContentsMargins(14, 12, 14, 12)
        detail_layout.setSpacing(14)
        self.hero_portrait = QLabel('?')
        self.hero_portrait.setObjectName('heroPortrait')
        self.hero_portrait.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hero_portrait.setFixedSize(76, 76)
        detail_layout.addWidget(self.hero_portrait)
        identity_layout = QVBoxLayout()
        identity_layout.setSpacing(3)
        self.detail_player_name = QLabel('Выбери игрока')
        self.detail_player_name.setObjectName('playerName')
        self.detail_hero_name = QLabel('Портрет и финальная сборка')
        self.detail_hero_name.setObjectName('playerHero')
        self.detail_summary = QLabel('—')
        self.detail_summary.setObjectName('playerMeta')
        identity_layout.addWidget(self.detail_player_name)
        identity_layout.addWidget(self.detail_hero_name)
        identity_layout.addWidget(self.detail_summary)
        detail_layout.addLayout(identity_layout)
        detail_layout.addStretch()
        inventory_layout = QVBoxLayout()
        inventory_title = QLabel('ФИНАЛЬНЫЙ ИНВЕНТАРЬ')
        inventory_title.setObjectName('cardTitle')
        inventory_layout.addWidget(inventory_title)
        slots_layout = QHBoxLayout()
        slots_layout.setSpacing(7)
        self.inventory_slots: list[QLabel] = []
        for _ in range(6):
            slot = QLabel('—')
            slot.setObjectName('itemSlot')
            slot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            slot.setFixedSize(54, 54)
            self.inventory_slots.append(slot)
            slots_layout.addWidget(slot)
        inventory_layout.addLayout(slots_layout)
        detail_layout.addLayout(inventory_layout)
        layout.addWidget(detail)
        self.stats_table = QTableWidget(0, 17)
        self.stats_table.setHorizontalHeaderLabels(['Игрок', 'Герой', 'Сторона', 'Итог', 'K', 'D', 'A', 'Крипы', 'Денаи', 'Нейтралы', 'Золото', 'Инвентарь', 'Net worth', 'APM сред.', 'APM пик', 'Пик на', 'Башни / Rax'])
        self._configure_table(self.stats_table)
        self.stats_table.setIconSize(QSize(36, 36))
        self.stats_table.verticalHeader().setDefaultSectionSize(46)
        self.stats_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.stats_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.stats_table)
        self.stats_table.itemSelectionChanged.connect(self._stats_selection_changed)
        self.stats_sections.addTab(players_page, 'Игроки')
        self.stats_sections.addTab(self._build_chat_page(), 'Чат')
        outer_layout.addWidget(self.stats_sections, 1)
        return tab

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
        return tab

    def _build_transport_bar(self) -> QWidget:
        transport = QFrame()
        transport.setObjectName('playerDetail')
        layout = QVBoxLayout(transport)
        layout.setContentsMargins(12, 8, 12, 8)
        seeker_bar = QHBoxLayout()
        self.attach_button = QPushButton('Подключить Seeker')
        self.attach_button.setVisible(False)
        self.seek_button = QPushButton('Перейти к таймингу')
        self.cancel_button = QPushButton('Стоп')
        self.seek_profile = QComboBox()
        for profile in SEEK_PROFILES.values():
            self.seek_profile.addItem(profile.label, profile.key)
        stored_profile = str(self.settings.value('seek_profile', 'balanced'))
        profile_index = self.seek_profile.findData(stored_profile)
        self.seek_profile.setCurrentIndex(profile_index if profile_index >= 0 else 1)
        self.seek_profile.setToolTip('Eco снижает нагрев. Balanced ограничен 32x. Maximum временно запрашивает HighQoS и использует максимум Warcraft.')
        self.seek_profile.currentIndexChanged.connect(lambda: self.settings.setValue('seek_profile', self.seek_profile.currentData()))
        self.seek_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.connection_label = QLabel('Warcraft не подключён')
        self.connection_label.setObjectName('connectionOffline')
        seeker_bar.addWidget(self.attach_button)
        seeker_bar.addWidget(self.seek_button)
        seeker_bar.addWidget(self.cancel_button)
        seeker_bar.addWidget(self.seek_profile)
        seeker_bar.addStretch()
        seeker_bar.addWidget(self.connection_label)
        layout.addLayout(seeker_bar)
        time_bar = QHBoxLayout()
        self.time_input = QLineEdit('00:00')
        self.time_input.setPlaceholderText('34:18')
        self.time_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.time_input.setFixedWidth(96)
        self.time_input.setToolTip('Формат: 34:18 или 1:02:03')
        self.timeline = TimelineSlider()
        self.end_label = QLabel('00:00')
        self.end_label.setObjectName('timeLabel')
        time_bar.addWidget(self.time_input)
        time_bar.addWidget(self.timeline, 1)
        time_bar.addWidget(self.end_label)
        layout.addLayout(time_bar)
        self.seek_status = QLabel('Выбери событие или введи точный тайминг.')
        self.seek_status.setObjectName('hint')
        layout.addWidget(self.seek_status)
        self.seek_metrics_label = QLabel('Seeker подключается автоматически после запуска реплея.')
        self.seek_metrics_label.setObjectName('hint')
        layout.addWidget(self.seek_metrics_label)
        self.attach_button.clicked.connect(lambda: self.seeker.attach_to_warcraft())
        self.seek_button.clicked.connect(self.seek_to_target)
        self.cancel_button.clicked.connect(self.seeker.cancel)
        self.timeline.valueChanged.connect(lambda value: self.time_input.setText(format_time(value)))
        self.time_input.returnPressed.connect(self._time_input_submitted)
        return transport

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
        description = QLabel('Камера получает мягкий разгон и торможение, не зависящие от FPS. Управление работает, пока активным окном является Warcraft.')
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
        self.camera_macro_combos: dict[str, QComboBox] = {}
        self._camera_binding_values: dict[str, int] = {}
        action_defaults = {action: default for action, _, default in CAMERA_MACRO_ACTIONS}
        self.camera_tool_tabs = QTabWidget()
        self.camera_tool_tabs.setObjectName('cameraToolTabs')
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
        drone_intro = QLabel('Свободный нативный полёт с инерцией и автоматическим креном. Захват цели использует выбранного героя: движение вперёд становится наездом, стрейф — ручным облётом, а Orbit ведёт круг автоматически и плавно переходит между тремя радиусами.')
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
        drone_help = QLabel('Стрелки — полёт / орбита · Insert/Delete — поворот · Home/End — наклон · Page Up/Page Down — наезд · Num 7/Num 9 — кольцо ближе/дальше · Num 8/Num 2 — Orbit и реверс · высота и разворот на 180° назначаются выше. При потере пакетов дрон сам тормозит.')
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
        layout.addWidget(panel)
        layout.addStretch()
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
        signals.scan_progress.connect(lambda value: self.seek_status.setText(f'Ищу активный реплей в памяти Warcraft… {value}%'))
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
        item.setToolTip(str(resolved))
        item.setData(Qt.ItemDataRole.UserRole, str(resolved))
        self.replay_list.addItem(item)
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

    def _launch_replay_in_warcraft(self, path: Path) -> bool:
        if self._launch_task is not None:
            self.seek_status.setText('Другой replay уже запускается; дождись подтверждения.')
            return False
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
        self.connection_label.setText(f'Replay запущен · PID {pid}' if launch_verified else f'Warcraft запускается · PID {pid}')
        self.connection_label.setObjectName('connectionOffline')
        self.connection_label.style().unpolish(self.connection_label)
        self.connection_label.style().polish(self.connection_label)
        if launch_verified:
            self.seek_status.setText(f'{path.name} запущен {launch_mode}; живой replay подтверждён.')
        else:
            self.seek_status.setText(f'{path.name} открыт {launch_mode}. Seeker подключится после загрузки автоматически.')
        self._schedule_auto_attach(pid, delay_ms=150 if launch_verified else 2500)
        if self._pending_backward_seek is not None:
            self.seek_status.setText('Возврат назад · replay перезапущен, подключаюсь…')

    def _launch_failed(self, message: str) -> None:
        self._auto_attach_pid = None
        self.attach_button.setVisible(True)
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
            self.seek_status.setText('Replay запущен, но Seeker не успел подключиться. Попробуй ручное восстановление.')
            self.attach_button.setVisible(True)
            return
        if self.seeker.busy:
            QTimer.singleShot(250, self._auto_attach_seeker)
            return
        self.seek_status.setText('Replay запущен · готовлю Instant Seek…')
        self.seeker.attach_to_warcraft(pid, quiet=True)

    def load_replay(self, path: Path) -> None:
        self.current_path = path.resolve()
        self.report = None
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
        forget_failed_replay(self.settings, self.current_path)
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
        self._fill_stats(report)
        self._fill_camera_players(report)
        self._fill_moments(report, game_duration)
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
            inventory_tooltip = '\n'.join(item_lines) or 'Пустой инвентарь'
            result = 'Победа' if player.won is True else 'Поражение' if player.won is False else '—'
            creep_values = [number(player.creep_kills), number(player.creep_denies), number(player.neutral_kills)]
            creep_tooltip: str | None = None
            if player.creep_stats_source == 'periodic-snapshot':
                creep_values = [f'≈{value}' if value != '—' else value for value in creep_values]
                creep_tooltip = f'Последний доступный срез карты на {format_time(player.creep_stats_game_time_ms)}. Финальный блок статистики не был записан.'
            elif player.creep_stats_source == 'final':
                creep_tooltip = 'Финальная статистика карты.'
            values = [player.name, player.hero_name or player.hero_rawcode or '—', player.side or '—', result, number(player.kills), number(player.deaths), number(player.assists), *creep_values, number(player.final_gold), number(player.inventory_value), number(player.net_worth), '—' if player.apm_average is None else f'{player.apm_average:.1f}', number(player.apm_peak_60s), format_time(player.apm_peak_game_time_ms), f'{number(player.tower_kills)} / {number(player.rax_kills)}']
            for column, value in enumerate(values):
                tooltip = inventory_tooltip if column in (11, 12) else creep_tooltip if column in (7, 8, 9) else None
                item = table_item(value, tooltip=tooltip)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, player.slot)
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
        heroes: list[tuple[str, tuple[int, str, str]]] = []
        for player in report.dota_players:
            if player.hero_rawcode:
                hero = player.hero_name or player.hero_rawcode
                label = f'{player.name} · {hero}'
                heroes.append((label, (player.slot, player.hero_rawcode, label)))
        for slot_index, combo in enumerate(self.camera_hero_slots):
            combo.clear()
            for label, data in heroes:
                combo.addItem(label, data)
            if combo.count():
                combo.setCurrentIndex(min(slot_index, combo.count() - 1))
        self.camera_follow_button.setEnabled(self.camera_service.running and bool(heroes))

    def _show_player_detail(self, player: DotaPlayer) -> None:
        portrait = scaled_pixmap(hero_icon_path(player.hero_name), 72)
        self.hero_portrait.clear()
        if portrait is not None:
            self.hero_portrait.setPixmap(portrait)
        else:
            self.hero_portrait.setText('?')
        result = 'ПОБЕДА' if player.won is True else 'ПОРАЖЕНИЕ' if player.won is False else 'РЕЗУЛЬТАТ НЕИЗВЕСТЕН'
        self.detail_player_name.setText(player.name)
        self.detail_hero_name.setText(f"{player.hero_name or player.hero_rawcode or 'Неизвестный герой'} · {player.side or '—'} · {result}")
        average_apm = '—' if player.apm_average is None else f'{player.apm_average:.1f}'
        self.detail_summary.setText(f'K/D/A {number(player.kills)}/{number(player.deaths)}/{number(player.assists)}   ·   Net worth {number(player.net_worth)}   ·   APM {average_apm} / пик {number(player.apm_peak_60s)}')
        for index, slot_label in enumerate(self.inventory_slots):
            rawcode = player.final_item_rawcodes[index] if index < len(player.final_item_rawcodes) else None
            item_name = player.final_item_names[index] if index < len(player.final_item_names) else None
            cost = player.final_item_costs[index] if index < len(player.final_item_costs) else None
            slot_label.clear()
            icon = scaled_pixmap(item_icon_path(item_name), 50)
            if icon is not None:
                slot_label.setPixmap(icon)
            elif rawcode:
                slot_label.setText(rawcode)
            else:
                slot_label.setText('—')
            tooltip = item_name or rawcode or 'Пустой слот'
            if cost is not None and rawcode:
                tooltip += f'\nСтоимость: {number(cost)} gold'
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
        self.tabs.setCurrentIndex(0)
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
        if not self._launch_replay_in_warcraft(self.current_path):
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
        self._pending_backward_profile = None
        self._pending_backward_deadline = 0.0
        self._pending_attach_attempt = False

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
        self.camera_status.setText('Ищу камеру Warcraft в памяти…')
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

    def _prepare_camera_hero_slot(self, slot_index: int) -> None:
        if not self.camera_service.running or not 0 <= slot_index < len(self.camera_hero_slots):
            return
        data = self.camera_hero_slots[slot_index].currentData()
        if isinstance(data, (tuple, list)) and len(data) == 3:
            self.camera_service.prepare_hero_slots([(int(data[0]), str(data[1]))])

    def _camera_macro_triggered(self, action: str) -> None:
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
            self._follow_camera_slot(slot_index)

    def _sync_orbit_ring_controls(self, active: bool, ring_index: int) -> str:
        selected = min(max(int(ring_index), 0), 2)
        self.camera_orbit_in_button.setEnabled(active and selected > 0)
        self.camera_orbit_out_button.setEnabled(active and selected < 2)
        return ORBIT_RING_LABELS[selected]

    def _camera_ready(self, state: object) -> None:
        self.camera_start_button.setEnabled(False)
        self.camera_stop_button.setEnabled(True)
        self.camera_preset.setEnabled(True)
        self.camera_follow_button.setEnabled(self.camera_follow_player.count() > 0)
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
        update_hz = self.camera_service.native_update_hz
        self.camera_status.setText(f'Камера активна · нативная плавность {update_hz} Гц · защита границ включена')
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
            update_hz = self.camera_service.native_update_hz
            if self.camera_service.orbit_enabled:
                direction = 'влево' if self.camera_service.orbit_direction > 0 else 'вправо'
                ring_label = self._sync_orbit_ring_controls(True, self.camera_service.orbit_ring_index)
                self.camera_status.setText(f'Orbit · {ring_label} орбита · облёт {direction} · нативная физика {update_hz} Гц')
                return
            lock_text = ' · захват цели' if self.camera_service.drone_target_locked else ' · свободный полёт'
            self.camera_status.setText(f'Fly Drone активен{lock_text} · нативная физика {update_hz} Гц')
            return
        if not self.camera_service.following:
            update_hz = self.camera_service.native_update_hz
            self.camera_status.setText(f'Камера активна · движение рассчитывается с частотой {update_hz} Гц')

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
            self.camera_status.setText(f'Камера активна · быстрых геройских слотов готово: {count}/{len(self.camera_hero_slots)}')

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
        self.camera_status.setText(friendly)
        QMessageBox.warning(self, 'Camera Engine', friendly)

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
        if operation == 'attach' and self._pending_backward_seek is not None and (not self.seeker.attached):
            self._pending_attach_attempt = False
            QTimer.singleShot(1500, self._attach_for_backward_seek)

    def _seeker_attached(self, result: AttachResult) -> None:
        self._auto_attach_pid = None
        build_note = ' · совместимая Game.dll' if result.game_dll_match == 'layout-compatible' else ''
        self.connection_label.setText(f'Подключён · PID {result.pid} · {format_time(result.replay_position_ms)}{build_note}')
        self.connection_label.setObjectName('connectionOnline')
        self.connection_label.style().unpolish(self.connection_label)
        self.connection_label.style().polish(self.connection_label)
        self.attach_button.setVisible(False)
        self.seek_button.setEnabled(self.report is not None)
        self.seek_status.setText(f"{result.build_profile or 'Warcraft III 1.26a'} · подключено")
        cache_note = ' · cache' if result.validation_cache_hit or result.replay_scan_strategy in {'open-session', 'session-cache'} else ''
        self.seek_metrics_label.setText(f"Instant Seek готов за {result.attach_duration_ms:.0f} мс{cache_note} · replay block: {result.replay_scan_strategy or 'validated'}")
        if self._pending_backward_seek is not None:
            target = self._pending_backward_seek
            profile = self._pending_backward_profile or SEEK_PROFILES['balanced']
            self._clear_backward_seek()
            self.seek_status.setText('Реплей перезапущен · перематываю к точке назад…')
            QTimer.singleShot(100, lambda: self._seek_after_backward_attach(target, profile))

    def _seek_progress(self, progress: SeekProgress) -> None:
        game_start = self.report.game_start_ms if self.report else 0
        game_position = max(progress.current_replay_time_ms - (game_start or 0), 0)
        self.timeline.setValue(min(game_position, self.timeline.maximum()))
        stage = {'starting': 'запуск', 'cruise': 'разгон', 'braking': 'торможение'}.get(progress.stage, progress.stage)
        details = [f'Сейчас {format_time(game_position, millis=True)}', stage, f'лимит {format_seek_speed(progress.speed_value)}']
        if progress.effective_speed > 0.01:
            details.append(f'реально {progress.effective_speed:.1f}x')
        if progress.eta_seconds is not None:
            details.append(f'ETA {progress.eta_seconds:.1f} с')
        self.seek_status.setText(' · '.join(details))
        metrics = [f'команда {progress.command_latency_ms:.1f} мс']
        if progress.first_advance_ms is not None:
            metrics.append(f'первый тик {progress.first_advance_ms:.0f} мс')
        if progress.process_cpu_percent is not None:
            metrics.append(f'CPU {progress.process_cpu_percent:.0f}% одного ядра')
        self.seek_metrics_label.setText(' · '.join(metrics))

    def _seek_metrics(self, metrics: SeekMetrics) -> None:
        cpu = f' · CPU {metrics.process_cpu_percent:.0f}% одного ядра' if metrics.process_cpu_percent is not None else ''
        qos = ' · HighQoS' if metrics.high_qos_applied else ''
        first_tick = f'{metrics.first_advance_ms:.0f} мс' if metrics.first_advance_ms is not None else '—'
        self.seek_metrics_label.setText(f'{metrics.wall_duration_ms / 1000.0:.2f} с · {metrics.effective_speed:.1f}x · первый тик {first_tick} · перелёт {metrics.overshoot_ms} мс{cpu}{qos}')

    def _seek_done(self, replay_position: int) -> None:
        game_start = self.report.game_start_ms if self.report else 0
        game_position = max(replay_position - (game_start or 0), 0)
        self.timeline.setValue(min(game_position, self.timeline.maximum()))
        self.seek_status.setText(f'{format_time(game_position, millis=True)} · пауза')

    def _seeker_error(self, message: str) -> None:
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
            self.attach_button.setVisible(True)
        QMessageBox.warning(self, 'Replay Seeker', friendly)

    def _seeker_soft_error(self, message: str) -> None:
        if self._auto_attach_pid is not None and time.monotonic() < self._auto_attach_deadline:
            self.seek_status.setText('Warcraft ещё готовит replay · повторяю подключение…')
            if self._pending_backward_seek is None:
                QTimer.singleShot(750, self._auto_attach_seeker)
            return
        self._auto_attach_pid = None
        self.seek_status.setText(f'Instant Seek пока не готов: {message}')
        self.attach_button.setVisible(True)

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
        self.camera_service.shutdown()
        self.camera_input.stop()
        self.seeker.shutdown()
        super().closeEvent(event)

    def _apply_style(self) -> None:
        self.setStyleSheet('\n            QMainWindow, QWidget {\n                background: #0d1118;\n                color: #dce4ef;\n                font-family: "Segoe UI";\n                font-size: 10pt;\n            }\n            QLabel#appTitle {\n                color: #f3f6fb;\n                font-size: 17pt;\n                font-weight: 800;\n                letter-spacing: 2px;\n            }\n            QFrame#sidebar, QFrame#statCard, QFrame#playerDetail {\n                background: #141b25;\n                border: 1px solid #222c3a;\n                border-radius: 8px;\n            }\n            QFrame#transitionCard {\n                background: #111923;\n                border: 1px solid #263244;\n                border-radius: 7px;\n            }\n            QFrame#transitionCard QLabel {\n                background: transparent;\n                border: 0;\n            }\n            QLabel#cardTitle {\n                color: #7f8c9e;\n                font-size: 8pt;\n                font-weight: 700;\n            }\n            QLabel#cardValue {\n                color: #f3f6fb;\n                font-size: 13pt;\n                font-weight: 700;\n            }\n            QLabel#heroPortrait {\n                background: #0b1017;\n                border: 2px solid #33465e;\n                border-radius: 8px;\n                color: #67778b;\n                font-size: 22pt;\n                font-weight: 800;\n            }\n            QLabel#playerName {\n                color: #f4f7fb;\n                font-size: 15pt;\n                font-weight: 800;\n            }\n            QLabel#playerHero {\n                color: #55a7ff;\n                font-size: 10pt;\n                font-weight: 650;\n            }\n            QLabel#playerMeta { color: #96a5b8; }\n            QLabel#itemSlot {\n                background: #0a0f16;\n                border: 1px solid #334154;\n                border-radius: 6px;\n                color: #718097;\n                font-family: "Cascadia Mono";\n                font-size: 8pt;\n                font-weight: 700;\n            }\n            QLineEdit, QDoubleSpinBox {\n                background: #0a0f16;\n                border: 1px solid #334154;\n                border-radius: 6px;\n                color: #f1f5fb;\n                padding: 7px 9px;\n                font-family: "Cascadia Mono";\n                font-size: 10pt;\n                font-weight: 700;\n            }\n            QLineEdit:focus, QDoubleSpinBox:focus {\n                border-color: #2f7ef8;\n            }\n            QPushButton {\n                background: #1d6ff2;\n                border: 0;\n                border-radius: 6px;\n                padding: 8px 14px;\n                color: white;\n                font-weight: 600;\n            }\n            QPushButton:hover { background: #3181fa; }\n            QPushButton:pressed { background: #165dcf; }\n            QPushButton:disabled {\n                background: #252d39;\n                color: #687486;\n            }\n            QListWidget, QTableWidget {\n                background: #111720;\n                alternate-background-color: #151d28;\n                border: 1px solid #222c3a;\n                border-radius: 6px;\n                gridline-color: #202a37;\n                selection-background-color: #204d86;\n                selection-color: white;\n            }\n            QListWidget::item {\n                padding: 9px 7px;\n                border-radius: 4px;\n            }\n            QListWidget::item:selected { background: #204d86; }\n            QHeaderView::section {\n                background: #18212d;\n                color: #94a2b5;\n                border: 0;\n                border-right: 1px solid #273242;\n                padding: 8px;\n                font-weight: 700;\n            }\n            QTabWidget::pane {\n                border: 1px solid #222c3a;\n                border-radius: 7px;\n                background: #10161f;\n            }\n            QTabBar::tab {\n                background: #141b25;\n                color: #8492a6;\n                padding: 10px 18px;\n                margin-right: 2px;\n                border-top-left-radius: 6px;\n                border-top-right-radius: 6px;\n            }\n            QTabBar::tab:selected {\n                background: #1a2635;\n                color: #f0f5fb;\n            }\n            QLabel#hint, QLabel#statusLabel { color: #8492a6; }\n            QLabel#timeLabel {\n                color: #f0f5fb;\n                font-family: "Cascadia Mono";\n                font-weight: 700;\n            }\n            QLabel#connectionOffline { color: #ff8a78; }\n            QLabel#connectionOnline { color: #63d99a; }\n            QSlider::groove:horizontal {\n                height: 6px;\n                background: #2a3544;\n                border-radius: 3px;\n            }\n            QSlider::sub-page:horizontal {\n                background: #347fe8;\n                border-radius: 3px;\n            }\n            QSlider::handle:horizontal {\n                background: #f4f7fb;\n                border: 3px solid #347fe8;\n                width: 14px;\n                margin: -7px 0;\n                border-radius: 10px;\n            }\n            QSplitter::handle { background: transparent; width: 10px; }\n            QScrollBar:vertical, QScrollBar:horizontal {\n                background: #10161f;\n                border: 0;\n            }\n            QScrollBar::handle:vertical, QScrollBar::handle:horizontal {\n                background: #344154;\n                border-radius: 4px;\n                min-height: 24px;\n                min-width: 24px;\n            }\n            ')

def main() -> int:
    self_test = '--self-test' in sys.argv
    replay_argument: Path | None = None
    if '--self-test-replay' in sys.argv:
        index = sys.argv.index('--self-test-replay')
        if index + 1 < len(sys.argv):
            replay_argument = Path(sys.argv[index + 1])
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName('ReplayLab')
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
        expected_product_tabs = ('Просмотр', 'Съёмка', 'Статистика')
        expected_statistics_tabs = ('Игроки', 'Чат')
        actual_product_tabs = tuple((window.tabs.tabText(index) for index in range(window.tabs.count())))
        actual_statistics_tabs = tuple((window.stats_sections.tabText(index) for index in range(window.stats_sections.count())))
        actual_camera_tabs = tuple((window.camera_tool_tabs.tabText(index) for index in range(window.camera_tool_tabs.count())))
        if actual_product_tabs != expected_product_tabs or actual_statistics_tabs != expected_statistics_tabs or actual_camera_tabs != expected_camera_tabs or (len(window.camera_hero_slots) != CAMERA_HERO_SLOT_COUNT) or (len(window.camera_transition_buttons) != len(CAMERA_TRANSITION_ACTIONS)):
            window.close()
            return 23

        def exercise_tabs() -> None:
            for tab_index in range(window.tabs.count()):
                window.tabs.setCurrentIndex(tab_index)
                app.processEvents()
            for tool_index in range(window.camera_tool_tabs.count()):
                window.camera_tool_tabs.setCurrentIndex(tool_index)
                app.processEvents()
        window.show()
        app.processEvents()
        exercise_tabs()
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
            if hero_count <= 0 or not window._replay_moments or window.chat_table.rowCount() <= 0:
                window.close()
                return 24
            for hero_slot in window.camera_hero_slots:
                data = hero_slot.currentData()
                if hero_slot.count() != hero_count or not isinstance(data, (tuple, list)) or len(data) != 3:
                    window.close()
                    return 25
            exercise_tabs()
        window.close()
        app.processEvents()
        return 0
    window.show()
    return app.exec()
if __name__ == '__main__':
    raise SystemExit(main())
