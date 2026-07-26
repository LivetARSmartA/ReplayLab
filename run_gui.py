from __future__ import annotations
import sys
from pathlib import Path
project_root = Path(__file__).resolve().parent
for dependency_folder in ('.deps', '.gui_deps'):
    local_dependencies = project_root / dependency_folder
    if local_dependencies.is_dir():
        sys.path.insert(0, str(local_dependencies))
from w3g_parser.gui import main
if __name__ == '__main__':
    raise SystemExit(main())
