from __future__ import annotations
import concurrent.futures
import ctypes
import math
import os
import threading
from ctypes import wintypes
from dataclasses import dataclass
from typing import Mapping, Sequence
from PySide6.QtCore import QObject, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget
from .ability_profile import AbilityDefinition
from .assets import ability_icon_path, command_icon_path
from .native_telemetry import LiveAbilityState, NativeTelemetryHost, TelemetryHostError, TelemetrySnapshot
from .seeker import SeekBackendError
HUD_POLL_SECONDS = 0.05
TRANSIENT_TELEMETRY_STATUSES = frozenset({5, 6})
COMMAND_CARD_COLUMNS = 4

class AbilityTelemetrySignals(QObject):
    operation_started = Signal()
    ready = Signal(object)
    snapshot = Signal(object)
    transient = Signal(str)
    failed = Signal(str)
    stopped = Signal()

class AbilityTelemetryService(QObject):

    def __init__(self) -> None:
        super().__init__()
        self.signals = AbilityTelemetrySignals()
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='war3-ability-telemetry')
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._future: concurrent.futures.Future[None] | None = None
        self._pending_target: tuple[int, str] | None = None
        self._process_id = 0
        self._running = False

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._future is not None and (not self._future.done())

    def start(self, player_slot: int, hero_rawcode: str, *, process_id: int=0) -> None:
        with self._lock:
            if self._future is not None and (not self._future.done()):
                self.signals.failed.emit('Skills HUD уже подключается или работает.')
                return
            self._stop.clear()
            self._pending_target = None
            self._process_id = process_id
            self._running = False
            self._future = self._executor.submit(self._run, player_slot, hero_rawcode, process_id)
        self.signals.operation_started.emit()

    def set_target(self, player_slot: int, hero_rawcode: str) -> None:
        with self._lock:
            if self._future is None or self._future.done():
                raise SeekBackendError('Сначала включи Skills HUD.')
            self._pending_target = (player_slot, hero_rawcode)

    def stop(self) -> None:
        self._stop.set()

    def _take_pending_target(self) -> tuple[int, str] | None:
        with self._lock:
            target = self._pending_target
            self._pending_target = None
            return target

    def _run(self, player_slot: int, hero_rawcode: str, process_id: int) -> None:
        host: NativeTelemetryHost | None = None
        try:
            host = NativeTelemetryHost(player_slot, hero_rawcode, process_id=process_id)
            first = host.snapshot()
            with self._lock:
                self._running = True
            self.signals.ready.emit(first)
            self.signals.snapshot.emit(first)
            while not self._stop.wait(HUD_POLL_SECONDS):
                target = self._take_pending_target()
                try:
                    snapshot = host.set_target(*target) if target is not None else host.snapshot()
                except TelemetryHostError as exc:
                    if exc.status in TRANSIENT_TELEMETRY_STATUSES:
                        if target is not None:
                            with self._lock:
                                self._pending_target = target
                        self.signals.transient.emit(str(exc))
                        self._stop.wait(0.2)
                        continue
                    raise
                self.signals.snapshot.emit(snapshot)
        except (SeekBackendError, OSError, ValueError) as exc:
            if not self._stop.is_set():
                self.signals.failed.emit(str(exc))
        except Exception as exc:
            if not self._stop.is_set():
                self.signals.failed.emit(f'Неожиданная ошибка Skills HUD: {exc}')
        finally:
            if host is not None:
                host.close()
            with self._lock:
                self._running = False
                self._pending_target = None
            self.signals.stopped.emit()

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            future = self._future
        if future is not None:
            try:
                future.result(timeout=3.0)
            except (concurrent.futures.TimeoutError, SeekBackendError, OSError, ValueError):
                pass
        self._executor.shutdown(wait=False, cancel_futures=False)

@dataclass(frozen=True)
class PresentedAbility:
    definition: AbilityDefinition
    state: LiveAbilityState

def select_presented_abilities(snapshot: TelemetrySnapshot, definitions: Mapping[str, AbilityDefinition], preferred_rawcodes: Sequence[str]) -> tuple[PresentedAbility | None, ...]:
    live = {ability.rawcode: ability for ability in snapshot.abilities}
    preferred = list(dict.fromkeys(preferred_rawcodes))
    preferred_rank = {rawcode: index for index, rawcode in enumerate(preferred)}
    by_slot: dict[int, list[str]] = {slot: [] for slot in range(COMMAND_CARD_COLUMNS)}
    ordered_rawcodes = (*preferred, *(ability.rawcode for ability in snapshot.abilities))
    for rawcode in ordered_rawcodes:
        definition = definitions.get(rawcode)
        if definition is None or definition.button_y != 2 or definition.button_x not in by_slot or (rawcode in by_slot[definition.button_x]):
            continue
        by_slot[definition.button_x].append(rawcode)
    result: list[PresentedAbility | None] = []
    for slot in range(COMMAND_CARD_COLUMNS):
        candidates = by_slot[slot]
        if not candidates:
            result.append(None)
            continue

        def candidate_score(rawcode: str) -> tuple[int, int, int, int]:
            state = live.get(rawcode)
            return (int(state is not None), state.level if state is not None else 0, int(state is not None and state.cooldown_ms > 0), preferred_rank.get(rawcode, -1))
        rawcode = max(candidates, key=candidate_score)
        state = live.get(rawcode) or LiveAbilityState(rawcode, 0, 0, 0)
        result.append(PresentedAbility(definitions[rawcode], state))
    return tuple(result)

def command_card_bottom_row_geometry(client_width: int, client_height: int) -> tuple[int, int, int, int]:
    x, y, width, height = command_card_geometry(client_width, client_height)
    row_height = round(height / 3.0)
    return (x, y + height - row_height, width, row_height)

def command_card_geometry(client_width: int, client_height: int) -> tuple[int, int, int, int]:
    width = max(1, round(client_width * 0.235))
    height = max(3, round(client_height / 4.0))
    right_margin = max(1, round(client_width * 0.003))
    bottom_margin = max(1, round(client_height * 0.002))
    return (client_width - width - right_margin, client_height - height - bottom_margin, width, height)

class AbilityHudWindow(QWidget):

    def __init__(self) -> None:
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
        super().__init__(None, flags)
        self.setObjectName('abilityHudOverlay')
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._active = False
        self._native_styles_applied = False
        self._process_id = 0
        self._target_label = ''
        self._definitions: Mapping[str, AbilityDefinition] = {}
        self._preferred_rawcodes: tuple[str, ...] = ()
        self._snapshot: TelemetrySnapshot | None = None
        self._abilities: tuple[PresentedAbility | None, ...] = (None, None, None, None)
        self._pixmap_cache: dict[str, QPixmap | None] = {}
        self._command_pixmap_cache: dict[str, QPixmap | None] = {}
        self._position_timer = QTimer(self)
        self._position_timer.setInterval(200)
        self._position_timer.timeout.connect(self._sync_geometry)

    @property
    def active(self) -> bool:
        return self._active

    def set_target(self, label: str, definitions: Mapping[str, AbilityDefinition], preferred_rawcodes: Sequence[str]) -> None:
        self._target_label = label
        self._definitions = definitions
        self._preferred_rawcodes = tuple(preferred_rawcodes)
        if self._snapshot is not None:
            self._abilities = select_presented_abilities(self._snapshot, self._definitions, self._preferred_rawcodes)
        self.update()

    def update_snapshot(self, snapshot: TelemetrySnapshot) -> None:
        self._snapshot = snapshot
        self._process_id = snapshot.process_id
        self._abilities = select_presented_abilities(snapshot, self._definitions, self._preferred_rawcodes)
        self.update()

    def set_active(self, active: bool) -> None:
        self._active = bool(active)
        if self._active:
            self._position_timer.start()
            self._sync_geometry()
        else:
            self._position_timer.stop()
            self.hide()

    @staticmethod
    def _foreground_client_rect(expected_process_id: int) -> tuple[int, int, int, int] | None:
        if os.name != 'nt' or expected_process_id <= 0:
            return None
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetClientRect.restype = wintypes.BOOL
        user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        user32.ClientToScreen.restype = wintypes.BOOL
        window = user32.GetForegroundWindow()
        if not window:
            return None
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(process_id))
        if int(process_id.value) != expected_process_id:
            return None
        client = wintypes.RECT()
        origin = wintypes.POINT()
        if not user32.GetClientRect(window, ctypes.byref(client)) or not user32.ClientToScreen(window, ctypes.byref(origin)):
            return None
        width = int(client.right - client.left)
        height = int(client.bottom - client.top)
        if width < 640 or height < 480:
            return None
        return (int(origin.x), int(origin.y), width, height)

    def _sync_geometry(self) -> None:
        if not self._active:
            return
        client = self._foreground_client_rect(self._process_id)
        if client is None:
            self.hide()
            return
        left, top, client_width, client_height = client
        x, y, width, height = command_card_geometry(client_width, client_height)
        self.setGeometry(left + x, top + y, width, height)
        if not self.isVisible():
            self.show()
            self._apply_native_styles()
        self.raise_()

    def _apply_native_styles(self) -> None:
        if os.name != 'nt' or self._native_styles_applied:
            return
        hwnd = int(self.winId())
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        get_style = user32.GetWindowLongPtrW
        set_style = user32.SetWindowLongPtrW
        get_style.argtypes = [wintypes.HWND, ctypes.c_int]
        get_style.restype = ctypes.c_ssize_t
        set_style.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        set_style.restype = ctypes.c_ssize_t
        ex_style = get_style(hwnd, -20)
        set_style(hwnd, -20, ex_style | 32 | 134217728 | 128)
        self._native_styles_applied = True

    @staticmethod
    def _cooldown_label(milliseconds: int) -> str:
        seconds = milliseconds / 1000.0
        if seconds >= 60.0:
            minutes, remainder = divmod(math.ceil(seconds), 60)
            return f'{minutes}:{remainder:02d}'
        if seconds >= 10.0:
            return str(math.ceil(seconds))
        return f'{seconds:.1f}'

    def _ability_pixmap(self, rawcode: str) -> QPixmap | None:
        if rawcode not in self._pixmap_cache:
            path = ability_icon_path(rawcode)
            pixmap = QPixmap(str(path)) if path is not None else QPixmap()
            self._pixmap_cache[rawcode] = pixmap if not pixmap.isNull() else None
        return self._pixmap_cache[rawcode]

    def _command_pixmap(self, command: str) -> QPixmap | None:
        if command not in self._command_pixmap_cache:
            path = command_icon_path(command)
            pixmap = QPixmap(str(path)) if path is not None else QPixmap()
            self._command_pixmap_cache[command] = pixmap if not pixmap.isNull() else None
        return self._command_pixmap_cache[command]

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        cell_width = self.width() / COMMAND_CARD_COLUMNS
        row_height = self.height() / 3.0
        painter.fillRect(self.rect(), QColor(1, 2, 2, 248))
        commands: tuple[tuple[str | None, ...], ...] = (('move', 'stop', 'hold', 'attack'), ('patrol', None, None, 'skill_menu'))
        for row in range(3):
            for column in range(COMMAND_CARD_COLUMNS):
                cell = QRectF(column * cell_width, row * row_height, cell_width, row_height)
                frame = cell.adjusted(2.0, 2.0, -2.0, -2.0)
                painter.fillRect(frame, QColor(5, 6, 6, 255))
                painter.setPen(QPen(QColor('#737878'), 2.0))
                painter.drawRect(frame)
                painter.setPen(QPen(QColor('#b2b6b4'), 1.0))
                painter.drawLine(frame.topLeft(), frame.topRight())
                painter.drawLine(frame.topLeft(), frame.bottomLeft())
                painter.setPen(QPen(QColor('#242827'), 2.0))
                painter.drawLine(frame.bottomLeft(), frame.bottomRight())
                painter.drawLine(frame.topRight(), frame.bottomRight())
                icon = QRectF(cell.left() + cell.width() * 0.075, cell.top() + cell.height() * 0.065, cell.width() * 0.85, cell.height() * 0.88)
                painter.fillRect(icon, QColor(0, 0, 0, 255))
                if row < 2:
                    command = commands[row][column]
                    if command is not None:
                        pixmap = self._command_pixmap(command)
                        if pixmap is not None:
                            painter.drawPixmap(icon, pixmap, QRectF(pixmap.rect()))
                    continue
                ability = self._abilities[column]
                if ability is None:
                    continue
                pixmap = self._ability_pixmap(ability.state.rawcode)
                if pixmap is not None:
                    painter.drawPixmap(icon, pixmap, QRectF(pixmap.rect()))
                if ability.state.level <= 0:
                    painter.fillRect(icon, QColor(0, 0, 0, 125))
                if ability.state.cooldown_ms > 0:
                    painter.fillRect(icon, QColor(0, 0, 0, 178))
                    cooldown_font = QFont('Arial', max(10, round(icon.height() * 0.29)))
                    cooldown_font.setBold(True)
                    painter.setFont(cooldown_font)
                    painter.setPen(QColor('#ffffff'))
                    painter.drawText(icon, Qt.AlignmentFlag.AlignCenter, self._cooldown_label(ability.state.cooldown_ms))
                if ability.state.level > 0:
                    badge_size = max(14.0, icon.height() * 0.24)
                    badge = QRectF(icon.right() - badge_size, icon.bottom() - badge_size, badge_size, badge_size)
                    painter.fillRect(badge, QColor(3, 3, 3, 235))
                    painter.setPen(QPen(QColor('#b98b32'), 1.0))
                    painter.drawRect(badge)
                    level_font = QFont('Arial', max(8, round(badge.height() * 0.55)))
                    level_font.setBold(True)
                    painter.setFont(level_font)
                    painter.setPen(QColor('#f4e7b0'))
                    painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, str(ability.state.level))
        painter.end()
