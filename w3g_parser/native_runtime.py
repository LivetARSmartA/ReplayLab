from __future__ import annotations
import os
import sys
from pathlib import Path

def native_binary_candidates(filename: str, *, environment_variable: str | None=None, build_subdirectory: str | None=None) -> list[Path]:
    candidates: list[Path] = []
    if environment_variable:
        configured = os.environ.get(environment_variable)
        if configured:
            candidates.append(Path(configured))
    bundle_root = getattr(sys, '_MEIPASS', None)
    if bundle_root:
        candidates.append(Path(bundle_root) / 'native' / filename)
    candidates.append(Path(sys.executable).resolve().parent / '_internal' / 'native' / filename)
    project_root = Path(__file__).resolve().parents[1]
    candidates.append(project_root / 'native' / filename)
    if build_subdirectory:
        candidates.append(project_root / 'build' / 'native' / build_subdirectory / filename)
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve()).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique
