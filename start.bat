@echo off
rem ============================================================
rem  SinanoServerCompanion launcher
rem   - Creates the .venv virtual environment on first run
rem   - Installs dependencies when requirements.txt has changed
rem   - Opens config.yaml in Notepad after main.py creates it on first run
rem ============================================================
setlocal
cd /d "%~dp0"
chcp 65001 >nul

rem Do not write __pycache__ and force UTF-8 console I/O
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "VENV_DIR=.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "REQ_FILE=requirements.txt"
set "REQ_STAMP=%VENV_DIR%\requirements.installed"

rem Create the virtual environment if it does not exist yet
if not exist "%VENV_PY%" (
    echo [INFO] Creating virtual environment in %VENV_DIR% ...
    py -3 -m venv "%VENV_DIR%" >nul 2>&1
    if not exist "%VENV_PY%" python -m venv "%VENV_DIR%"
)
if not exist "%VENV_PY%" (
    echo [ERROR] Failed to create the virtual environment.
    echo [ERROR] Install Python 3.10 or later and make sure "py" or "python" is on PATH.
    pause
    exit /b 1
)

rem Install dependencies only when requirements.txt differs from the last install
fc /b "%REQ_FILE%" "%REQ_STAMP%" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing dependencies ...
    "%VENV_PY%" -m pip install --disable-pip-version-check --no-compile -q -r "%REQ_FILE%"
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies.
        pause
        exit /b 1
    )
    copy /y "%REQ_FILE%" "%REQ_STAMP%" >nul
)

echo [INFO] Starting SinanoServerCompanion ...
"%VENV_PY%" -B main.py
set "EXIT_CODE=%ERRORLEVEL%"

rem Exit code 2 means main.py created config.yaml from config.example.yaml
if "%EXIT_CODE%"=="2" (
    echo [INFO] config.yaml has been created from config.example.yaml.
    echo [INFO] Fill in the bot token, server ID and role IDs, then run this file again.
    start "" notepad "config.yaml"
    pause
    endlocal & exit /b 0
)
if not "%EXIT_CODE%"=="0" (
    echo [ERROR] The bot exited with code %EXIT_CODE%.
    pause
)
endlocal & exit /b %EXIT_CODE%
