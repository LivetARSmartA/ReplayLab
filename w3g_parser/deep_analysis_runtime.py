from __future__ import annotations
import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable
from .deep_analysis import DeepAnalysisBundle
from .deep_analysis_sidecar import DeepAnalysisCache, bundle_from_json, sha256_file

class DeepAnalysisCaptureError(RuntimeError):
    pass

def _analysis_host_candidates() -> tuple[Path, ...]:
    names = ('replaylab_analysis_host.exe', 'replaylab_analysis_host')
    configured = os.environ.get('REPLAYLAB_ANALYSIS_HOST')
    local_app_data = os.environ.get('LOCALAPPDATA')
    roots = [Path(sys.executable).resolve().parent, Path(__file__).resolve().parents[1] / 'native' / 'bin', Path(__file__).resolve().parents[1] / 'build' / 'native']
    if local_app_data:
        roots.append(Path(local_app_data) / 'ReplayLab' / 'providers')
    candidates = [root / name for root in roots for name in names]
    if configured:
        candidates.insert(0, Path(configured))
    return tuple(candidates)

def find_analysis_host() -> Path:
    for candidate in _analysis_host_candidates():
        if candidate.is_file():
            return candidate
    raise DeepAnalysisCaptureError('Модуль захвата Deep Analysis не найден. ReplayLab не будет подменять GPM выборкой кошелька.')

def analysis_progress_from_host_row(row: object) -> tuple[int, str] | None:
    if not isinstance(row, dict):
        return None
    status = row.get('status')
    if status == 'native-armed':
        players = row.get('players')
        suffix = f' · игроков: {players}' if isinstance(players, int) else ''
        return (6, f'C++ capture подключён{suffix}')
    if status == 'starting':
        return (6, 'C++ capture инициализируется')
    if status == 'cancelling':
        return (6, 'C++ capture завершает сеанс')
    if status != 'running':
        return None
    position = row.get('maximum_position_ms', row.get('position_ms'))
    length = row.get('length_ms')
    if not isinstance(position, int) or not isinstance(length, int) or length <= 0:
        return None
    ratio = min(max(position / length, 0.0), 1.0)
    value = min(90, 6 + round(ratio * 84))
    return (value, f'Один C++ проход · {ratio:.0%} реплея')

class DeepAnalysisCoordinator:

    def __init__(self, cache: DeepAnalysisCache | None=None, host: Path | None=None) -> None:
        self.cache = cache or DeepAnalysisCache()
        self.host = host

    def run(self, replay_path: Path, warcraft_path: Path | None, iccup_path: Path | None, progress: Callable[[int, str], None], cancelled: threading.Event, *, use_cache: bool=True) -> DeepAnalysisBundle:
        replay_path = replay_path.resolve()
        if not replay_path.is_file():
            raise DeepAnalysisCaptureError('Выбранный реплей больше не существует.')
        progress(2, 'Проверяю отпечаток реплея')
        replay_sha256 = sha256_file(replay_path)
        if use_cache:
            cached = self.cache.latest_for_replay(replay_sha256)
            if cached is not None:
                progress(100, 'Проверенный sidecar найден в кэше')
                return cached
        if cancelled.is_set():
            raise DeepAnalysisCaptureError('Глубокий анализ отменён.')
        if warcraft_path is None or not warcraft_path.is_file():
            raise DeepAnalysisCaptureError('Проверенного sidecar нет. Укажи рабочий war3.exe в настройках запуска для нового capture.')
        host = (self.host or find_analysis_host()).resolve()
        self.cache.root.mkdir(parents=True, exist_ok=True)
        pending = self.cache.root / f'{replay_sha256}.capture.pending.json'
        cancel_file = self.cache.root / f'{replay_sha256}.capture.cancel'
        pending.unlink(missing_ok=True)
        cancel_file.unlink(missing_ok=True)
        command = [str(host), '--replay', str(replay_path), '--warcraft', str(warcraft_path.resolve()), '--output', str(pending), '--json-progress', '--cancel-file', str(cancel_file)]
        if iccup_path is not None:
            command.extend(('--iccup', str(iccup_path.resolve())))
        creation_flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', creationflags=creation_flags)
        output_lines: queue.Queue[str] = queue.Queue()
        stderr_lines: list[str] = []

        def read_stdout() -> None:
            if process.stdout is not None:
                for line in process.stdout:
                    output_lines.put(line)

        def read_stderr() -> None:
            if process.stderr is not None:
                stderr_lines.extend(process.stderr.read().splitlines())
        stdout_thread = threading.Thread(target=read_stdout, daemon=True)
        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        try:
            while process.poll() is None:
                if cancelled.is_set():
                    cancel_file.write_text('cancel\n', encoding='ascii')
                    try:
                        process.wait(timeout=10.0)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                    raise DeepAnalysisCaptureError('Глубокий анализ отменён.')
                try:
                    line = output_lines.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                value = row.get('progress')
                message = row.get('message')
                if isinstance(value, int) and isinstance(message, str):
                    progress(min(max(value, 0), 99), message)
                    continue
                native_progress = analysis_progress_from_host_row(row)
                if native_progress is not None:
                    progress(*native_progress)
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)
            if process.returncode != 0:
                detail = next((line.strip() for line in reversed(stderr_lines) if line.strip() and (not line.strip().startswith('provider wall time:'))), 'capture host returned an error')
                raise DeepAnalysisCaptureError(f'Deep Analysis остановлен: {detail}')
            if not pending.is_file():
                raise DeepAnalysisCaptureError('Модуль анализа завершился без sidecar.')
            bundle = bundle_from_json(pending)
            if bundle.identity.replay_sha256.lower() != replay_sha256:
                raise DeepAnalysisCaptureError('Sidecar относится к другому реплею.')
            if not bundle.is_valid:
                raise DeepAnalysisCaptureError('Sidecar не содержит ни одной валидной аналитической серии.')
            destination = self.cache.store(bundle)
            pending.unlink(missing_ok=True)
            progress(100, f'Анализ проверен · {destination.name[:12]}')
            return bundle
        finally:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
            if pending.is_file():
                pending.unlink(missing_ok=True)
            cancel_file.unlink(missing_ok=True)
