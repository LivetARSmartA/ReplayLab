@echo off
setlocal
set "PROJECT_DIR=%~dp0"
set "VENV_PYTHON=%PROJECT_DIR%.venv\Scripts\python.exe"
if exist "%VENV_PYTHON%" (
    "%VENV_PYTHON%" "%PROJECT_DIR%run_gui.py"
) else (
    python "%PROJECT_DIR%run_gui.py"
)
if errorlevel 1 pause
