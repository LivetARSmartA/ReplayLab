from __future__ import annotations
import concurrent.futures
import ctypes
import math
import os
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Mapping, Sequence
from PySide6.QtCore import QObject, QPointF, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QWidget
from .ability_profile import AbilityDefinition
from .assets import ability_icon_path, command_icon_path
from .diagnostics import get_logger
from .native_telemetry import LiveAbilityState, NativeTelemetryHost, TelemetryHostError, TelemetrySnapshot
from .seeker import SeekBackendError
HUD_POLL_SECONDS = 0.05
POINTER_SELECTION_SETTLE_SECONDS = 0.075
TRANSIENT_TELEMETRY_STATUSES = frozenset({5, 6})
COMMAND_CARD_COLUMNS = 4
COMMAND_CARD_ROWS = 3
COMMAND_CARD_CELLS = COMMAND_CARD_COLUMNS * COMMAND_CARD_ROWS
CURSOR_BRIDGE_INTERVAL_MS = 8
CURSOR_BRIDGE_WIDTH = 31
CURSOR_BRIDGE_HEIGHT = 40
CURSOR_BRIDGE_HOTSPOT = (3, 2)
LOGGER = get_logger('skills_hud')

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
        self._pending_target: tuple[int, str, int] | None = None
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

    def start(self, player_slot: int, hero_rawcode: str, *, process_id: int=0, targets: Sequence[tuple[int, str]]=()) -> None:
        with self._lock:
            if self._future is not None and (not self._future.done()):
                self.signals.failed.emit('Skills HUD уже подключается или работает.')
                return
            self._stop.clear()
            self._pending_target = None
            self._process_id = process_id
            self._running = False
            self._future = self._executor.submit(self._run, player_slot, hero_rawcode, process_id, tuple(targets))
        self.signals.operation_started.emit()

    def set_target(self, player_slot: int, hero_rawcode: str, hero_address: int=0) -> None:
        with self._lock:
            if self._future is None or self._future.done():
                raise SeekBackendError('Сначала включи Skills HUD.')
            self._pending_target = (player_slot, hero_rawcode, hero_address)

    def stop(self) -> None:
        self._stop.set()

    def _take_pending_target(self) -> tuple[int, str, int] | None:
        with self._lock:
            target = self._pending_target
            self._pending_target = None
            return target

    def _run(self, player_slot: int, hero_rawcode: str, process_id: int, targets: tuple[tuple[int, str], ...]) -> None:
        host: NativeTelemetryHost | None = None
        try:
            host = NativeTelemetryHost(player_slot, hero_rawcode, process_id=process_id)
            while True:
                try:
                    first = host.snapshot()
                    break
                except TelemetryHostError as exc:
                    if exc.status not in TRANSIENT_TELEMETRY_STATUSES:
                        raise
                    self.signals.transient.emit(str(exc))
                    if self._stop.wait(0.25):
                        return
            with self._lock:
                self._running = True
            self.signals.ready.emit(first)
            self.signals.snapshot.emit(first)
            if targets:
                prepared = host.prepare_targets(list(targets))
                self.signals.snapshot.emit(prepared)
            while not self._stop.wait(HUD_POLL_SECONDS):
                target = self._take_pending_target()
                try:
                    if target is None:
                        snapshot = host.snapshot()
                    else:
                        slot, rawcode, address = target
                        snapshot = host.set_target(slot, rawcode, address)
                except TelemetryHostError as exc:
                    if exc.status in TRANSIENT_TELEMETRY_STATUSES:
                        if target is not None:
                            with self._lock:
                                self._pending_target = target
                        if exc.snapshot is not None:
                            self.signals.snapshot.emit(exc.snapshot)
                        self.signals.transient.emit(str(exc))
                        self._stop.wait(0.2)
                        continue
                    raise
                self.signals.snapshot.emit(snapshot)
        except (SeekBackendError, OSError, ValueError) as exc:
            if not self._stop.is_set():
                LOGGER.error('Skills HUD service failed: %s', exc)
                self.signals.failed.emit(str(exc))
        except Exception:
            if not self._stop.is_set():
                LOGGER.exception('Unexpected Skills HUD service failure')
                self.signals.failed.emit('Skills HUD остановлен из-за внутренней ошибки.')
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

def _runtime_definition(rawcode: str, state: LiveAbilityState, *, button_x: int | None=None) -> AbilityDefinition:
    return AbilityDefinition(rawcode=rawcode, name=f'Способность {rawcode}', max_levels=max(state.level, 1), button_x=button_x, button_y=2 if button_x is not None else None)

class AbilityHudSelectionArbiter:

    def __init__(self) -> None:
        self._observed: tuple[int, int | None, str | None] | None = None
        self._pinned_player_slot: int | None = None
        self._pointer_selection_pending = False
        self._pointer_selection_baseline: tuple[int, int | None, str | None] | None = None
        self._pointer_selection_not_before = 0.0

    def observe(self, unit_address: int, player_slot: int | None, unit_rawcode: str | None, *, selectable: bool=True, now: float | None=None) -> bool:
        current = (unit_address, player_slot, unit_rawcode)
        self._observed = current
        if self._pinned_player_slot is None:
            return True
        if not self._pointer_selection_pending or not selectable:
            return False
        timestamp = time.monotonic() if now is None else now
        changed_away_from_programmatic_target = current != self._pointer_selection_baseline and player_slot != self._pinned_player_slot
        if not changed_away_from_programmatic_target and timestamp < self._pointer_selection_not_before:
            return False
        self._pinned_player_slot = None
        self._pointer_selection_pending = False
        self._pointer_selection_baseline = None
        self._pointer_selection_not_before = 0.0
        return True

    def pin_explicit_target(self, player_slot: int) -> None:
        self._pinned_player_slot = player_slot
        self._pointer_selection_pending = False
        self._pointer_selection_baseline = None
        self._pointer_selection_not_before = 0.0

    def begin_pointer_selection(self, *, now: float | None=None) -> None:
        if self._pinned_player_slot is None:
            return
        timestamp = time.monotonic() if now is None else now
        self._pointer_selection_pending = True
        self._pointer_selection_baseline = self._observed
        self._pointer_selection_not_before = timestamp + POINTER_SELECTION_SETTLE_SECONDS

    def clear(self) -> None:
        self._observed = None
        self._pinned_player_slot = None
        self._pointer_selection_pending = False
        self._pointer_selection_baseline = None
        self._pointer_selection_not_before = 0.0

def select_presented_abilities(snapshot: TelemetrySnapshot, definitions: Mapping[str, AbilityDefinition], preferred_rawcodes: Sequence[str]) -> tuple[PresentedAbility | None, ...]:
    live = {ability.rawcode: ability for ability in snapshot.abilities}
    preferred = list(dict.fromkeys(preferred_rawcodes))
    preferred_rank = {rawcode: index for index, rawcode in enumerate(preferred)}
    by_slot: dict[int, list[str]] = {slot: [] for slot in range(COMMAND_CARD_COLUMNS)}
    ordered_rawcodes = tuple(preferred) or tuple((ability.rawcode for ability in snapshot.abilities))
    for rawcode in ordered_rawcodes:
        definition = definitions.get(rawcode)
        if definition is None or definition.button_y != 2 or definition.button_x not in by_slot or (rawcode in by_slot[definition.button_x]):
            continue
        by_slot[definition.button_x].append(rawcode)
    result: list[PresentedAbility | None] = []
    selected_rawcodes: set[str] = set()
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
        selected_rawcodes.add(rawcode)
    fallback_rawcodes = []
    for rawcode in dict.fromkeys(ordered_rawcodes):
        if rawcode in selected_rawcodes:
            continue
        definition = definitions.get(rawcode)
        if definition is None:
            fallback_rawcodes.append(rawcode)
            continue
        if not preferred and (definition.button_y != 2 or definition.button_x not in by_slot):
            fallback_rawcodes.append(rawcode)
    for slot, ability in enumerate(result):
        if ability is not None or not fallback_rawcodes:
            continue
        rawcode = fallback_rawcodes.pop(0)
        state = live.get(rawcode) or LiveAbilityState(rawcode, 0, 0, 0)
        definition = definitions.get(rawcode) or _runtime_definition(rawcode, state, button_x=slot)
        result[slot] = PresentedAbility(definition, state)
    return tuple(result)

def select_presented_command_card(snapshot: TelemetrySnapshot, definitions: Mapping[str, AbilityDefinition], preferred_rawcodes: Sequence[str]) -> tuple[PresentedAbility | None, ...]:
    result: list[PresentedAbility | None] = [None] * COMMAND_CARD_CELLS
    result[COMMAND_CARD_COLUMNS * 2:] = select_presented_abilities(snapshot, definitions, preferred_rawcodes)
    live = {ability.rawcode: ability for ability in snapshot.abilities}
    for dynamic_slot, rawcode in enumerate(snapshot.invoked_spell_rawcodes[:2], start=1):
        state = live.get(rawcode) or LiveAbilityState(rawcode, 0, 0, 0)
        definition = definitions.get(rawcode) or _runtime_definition(rawcode, state)
        result[COMMAND_CARD_COLUMNS + dynamic_slot] = PresentedAbility(definition, state)
    return tuple(result)

def command_card_cell_at(x: float, y: float, width: float, height: float) -> int | None:
    if width <= 0 or height <= 0 or (not (0 <= x < width and 0 <= y < height)):
        return None
    column = min(int(x * COMMAND_CARD_COLUMNS / width), 3)
    row = min(int(y * COMMAND_CARD_ROWS / height), 2)
    return row * COMMAND_CARD_COLUMNS + column

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

def cursor_is_over_overlay(cursor_x: int, cursor_y: int, overlay_left: int, overlay_top: int, overlay_width: int, overlay_height: int) -> bool:
    return overlay_width > 0 and overlay_height > 0 and (overlay_left <= cursor_x < overlay_left + overlay_width) and (overlay_top <= cursor_y < overlay_top + overlay_height)

class AbilityHudCursorWindow(QWidget):

    def __init__(self) -> None:
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
        super().__init__(None, flags)
        self.setObjectName('abilityHudCursorBridge')
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(CURSOR_BRIDGE_WIDTH, CURSOR_BRIDGE_HEIGHT)
        self._native_styles_applied = False

    def show_at(self, cursor_x: int, cursor_y: int, *, raise_window: bool=False) -> None:
        hotspot_x, hotspot_y = CURSOR_BRIDGE_HOTSPOT
        next_x = cursor_x - hotspot_x
        next_y = cursor_y - hotspot_y
        if self.x() != next_x or self.y() != next_y:
            self.move(next_x, next_y)
        was_visible = self.isVisible()
        if not was_visible:
            self.show()
            self._apply_native_styles()
        if raise_window or not was_visible:
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

    def paintEvent(self, _event: object) -> None:
        pointer = QPainterPath(QPointF(3.0, 2.0))
        pointer.lineTo(3.0, 29.0)
        pointer.lineTo(9.0, 23.0)
        pointer.lineTo(15.0, 37.0)
        pointer.lineTo(21.0, 34.0)
        pointer.lineTo(15.0, 21.0)
        pointer.lineTo(27.0, 21.0)
        pointer.closeSubpath()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 150))
        painter.save()
        painter.translate(2.0, 2.0)
        painter.drawPath(pointer)
        painter.restore()
        painter.setPen(QPen(QColor(23, 17, 7, 245), 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(QColor('#f2d987'))
        painter.drawPath(pointer)
        painter.setPen(QPen(QColor('#fff4bd'), 1.0))
        painter.drawLine(QPointF(6.0, 7.0), QPointF(6.0, 23.0))
        painter.end()

class AbilityTooltipWindow(QWidget):

    def __init__(self) -> None:
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
        super().__init__(None, flags)
        self.setObjectName('abilityHudTooltip')
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(330, 76)
        self._ability: PresentedAbility | None = None
        self._native_styles_applied = False

    def show_ability(self, ability: PresentedAbility, anchor_x: int, command_card_top: int, client_left: int, client_top: int, client_width: int) -> None:
        self._ability = ability
        maximum_left = client_left + client_width - self.width() - 4
        left = min(max(anchor_x, client_left + 4), maximum_left)
        top = max(client_top + 4, command_card_top - self.height() - 8)
        self.move(left, top)
        if not self.isVisible():
            self.show()
            self._apply_native_styles()
        self.raise_()
        self.update()

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

    def paintEvent(self, _event: object) -> None:
        ability = self._ability
        if ability is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        panel = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
        painter.setPen(QPen(QColor('#b98b32'), 1.5))
        painter.setBrush(QColor(3, 5, 5, 242))
        painter.drawRoundedRect(panel, 4.0, 4.0)
        title_font = QFont('Arial', 12)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor('#f2d987'))
        painter.drawText(QRectF(12.0, 8.0, self.width() - 24.0, 24.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, ability.definition.name)
        details = f'Уровень {ability.state.level}/{ability.definition.max_levels}'
        if ability.state.cooldown_ms > 0:
            details += f' · КД {AbilityHudWindow._cooldown_label(ability.state.cooldown_ms)}'
        details += f' · {ability.state.rawcode}'
        detail_font = QFont('Arial', 10)
        painter.setFont(detail_font)
        painter.setPen(QColor('#e7e3d5'))
        painter.drawText(QRectF(12.0, 38.0, self.width() - 24.0, 24.0), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, details)
        painter.end()

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
        self._abilities: tuple[PresentedAbility | None, ...] = (None,) * COMMAND_CARD_CELLS
        self._pixmap_cache: dict[str, QPixmap | None] = {}
        self._command_pixmap_cache: dict[str, QPixmap | None] = {}
        self._tooltip = AbilityTooltipWindow()
        self._cursor_bridge = AbilityHudCursorWindow()
        self._position_timer = QTimer(self)
        self._position_timer.setInterval(50)
        self._position_timer.timeout.connect(self._sync_geometry)
        self._cursor_timer = QTimer(self)
        self._cursor_timer.setInterval(CURSOR_BRIDGE_INTERVAL_MS)
        self._cursor_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._cursor_timer.timeout.connect(self._sync_cursor_bridge)

    @property
    def active(self) -> bool:
        return self._active

    def set_target(self, label: str, definitions: Mapping[str, AbilityDefinition], preferred_rawcodes: Sequence[str]) -> None:
        self._target_label = label
        self._definitions = definitions
        self._preferred_rawcodes = tuple(preferred_rawcodes)
        if self._snapshot is not None:
            self._abilities = select_presented_command_card(self._snapshot, self._definitions, self._preferred_rawcodes)
        self.update()

    def update_snapshot(self, snapshot: TelemetrySnapshot) -> None:
        self._snapshot = snapshot
        self._process_id = snapshot.process_id
        self._abilities = select_presented_command_card(snapshot, self._definitions, self._preferred_rawcodes)
        self.update()

    def set_active(self, active: bool) -> None:
        self._active = bool(active)
        if self._active:
            self._position_timer.start()
            self._cursor_timer.start()
            self._sync_geometry()
        else:
            self._position_timer.stop()
            self._cursor_timer.stop()
            self._tooltip.hide()
            self._cursor_bridge.hide()
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
            self._tooltip.hide()
            self._cursor_bridge.hide()
            self.hide()
            return
        left, top, client_width, client_height = client
        x, y, width, height = command_card_geometry(client_width, client_height)
        self.setGeometry(left + x, top + y, width, height)
        if not self.isVisible():
            self.show()
            self._apply_native_styles()
        self.raise_()
        self._sync_tooltip(left, top, client_width)
        self._sync_cursor_bridge(force_raise=True)

    @staticmethod
    def _global_cursor_position() -> tuple[int, int] | None:
        if os.name != 'nt':
            return None
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetCursorPos.restype = wintypes.BOOL
        point = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            return None
        return (int(point.x), int(point.y))

    def _sync_tooltip(self, client_left: int, client_top: int, client_width: int) -> None:
        cursor = self._global_cursor_position()
        if cursor is None:
            self._tooltip.hide()
            return
        cell_index = command_card_cell_at(cursor[0] - self.x(), cursor[1] - self.y(), self.width(), self.height())
        ability = self._abilities[cell_index] if cell_index is not None else None
        if ability is None:
            self._tooltip.hide()
            return
        column = cell_index % COMMAND_CARD_COLUMNS
        anchor_x = round(self.x() + column * self.width() / COMMAND_CARD_COLUMNS)
        self._tooltip.show_ability(ability, anchor_x, self.y(), client_left, client_top, client_width)

    def _sync_cursor_bridge(self, *, force_raise: bool=False) -> None:
        if not self._active or not self.isVisible():
            self._cursor_bridge.hide()
            return
        cursor = self._global_cursor_position()
        if cursor is None or not cursor_is_over_overlay(cursor[0], cursor[1], self.x(), self.y(), self.width(), self.height()):
            self._cursor_bridge.hide()
            return
        self._cursor_bridge.show_at(*cursor, raise_window=force_raise)

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
        for row in range(COMMAND_CARD_ROWS):
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
                ability = self._abilities[row * COMMAND_CARD_COLUMNS + column]
                if ability is None and row < 2:
                    command = commands[row][column]
                    if command is not None:
                        pixmap = self._command_pixmap(command)
                        if pixmap is not None:
                            painter.drawPixmap(icon, pixmap, QRectF(pixmap.rect()))
                    continue
                if ability is None:
                    continue
                pixmap = self._ability_pixmap(ability.state.rawcode)
                if pixmap is not None:
                    painter.drawPixmap(icon, pixmap, QRectF(pixmap.rect()))
                else:
                    fallback_font = QFont('Arial', max(8, round(icon.height() * 0.17)))
                    fallback_font.setBold(True)
                    painter.setFont(fallback_font)
                    painter.setPen(QColor('#c9c9c9'))
                    painter.drawText(icon, Qt.AlignmentFlag.AlignCenter, ability.state.rawcode)
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
