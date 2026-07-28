from __future__ import annotations
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from .native_runtime import native_binary_candidates
PROBE_OUTPUT = re.compile('^(invalid-image|invalid-pattern|not-found|unique|ambiguous)\\trva=0x([0-9A-Fa-f]{8})\\tmatches=(\\d+)$')

class NativeSignatureError(RuntimeError):
    pass

@dataclass(frozen=True)
class NativeSignatureResult:
    status: str
    rva: int
    match_count: int

    @property
    def unique(self) -> bool:
        return self.status == 'unique'

def find_native_signature_probe() -> Path:
    for candidate in native_binary_candidates('replaylab_signature_probe.exe', environment_variable='REPLAYLAB_SIGNATURE_PROBE', build_subdirectory='signature_scanner'):
        if candidate.is_file():
            return candidate.resolve()
    raise NativeSignatureError('Нативный сканер сигнатур ReplayLab не найден.')

def parse_probe_output(output: str) -> NativeSignatureResult:
    match = PROBE_OUTPUT.fullmatch(output.strip())
    if match is None:
        raise NativeSignatureError(f'Нативный сканер вернул неизвестный ответ: {output!r}')
    return NativeSignatureResult(status=match.group(1), rva=int(match.group(2), 16), match_count=int(match.group(3)))

def probe_pe_signature(path: str | Path, pattern: str, *, all_sections: bool=False, executable: Path | None=None) -> NativeSignatureResult:
    pe_path = Path(path).resolve()
    if not pe_path.is_file():
        raise NativeSignatureError(f'PE-файл не найден: {pe_path}')
    probe = executable.resolve() if executable is not None else find_native_signature_probe()
    command = [str(probe), str(pe_path), pattern]
    if all_sections:
        command.append('--all-sections')
    completed = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if completed.returncode not in (0, 2, 3):
        raise NativeSignatureError(f'Нативный сканер аварийно завершился: {completed.returncode}')
    output = completed.stdout.strip()
    if not output:
        detail = completed.stderr.strip() or 'ответ отсутствует'
        raise NativeSignatureError(f'Нативный сканер отклонил запрос: {detail}')
    return parse_probe_output(output)
