@echo off
setlocal EnableExtensions
title Nova Personal Assistant - Windows Installer

echo ========================================================
echo   Installing Nova Personal Assistant on Windows
echo ========================================================
echo.

set "SRC_DIR=%~dp0"
if "%SRC_DIR:~-1%"=="\" set "SRC_DIR=%SRC_DIR:~0,-1%"
set "INSTALL_DIR=%LOCALAPPDATA%\Programs\NovaAssistant"
set "VENV_DIR=%INSTALL_DIR%\.venv"

if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

for %%F in (assistant.py nova_skills.py nova_voice.py nova_memory.py nova_calendar.py install_nova.py Install_Nova.bat .env.example README.md COMMANDS.md) do (
    if exist "%SRC_DIR%\%%F" copy /Y "%SRC_DIR%\%%F" "%INSTALL_DIR%\%%F" >nul
)

set "PY_CMD="
py -3.12 -c "import sys" >nul 2>&1
if %errorlevel%==0 set "PY_CMD=py -3.12"

if not defined PY_CMD (
    python -c "import sys; raise SystemExit(0 if (3,10) <= sys.version_info[:2] < (3,14) else 1)" >nul 2>&1
    if %errorlevel%==0 set "PY_CMD=python"
)

if not defined PY_CMD (
    echo Python 3.12 not found. Attempting automatic install via winget...
    winget --version >nul 2>&1
    if %errorlevel%==0 (
        winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
        py -3.12 -c "import sys" >nul 2>&1
        if %errorlevel%==0 set "PY_CMD=py -3.12"
    )
)

if not defined PY_CMD (
    echo.
    echo ERROR: Python 3.12 is required and could not be installed automatically.
    echo Please install Python 3.12 from https://www.python.org/downloads/
    echo Make sure to check "Add python.exe to PATH", then run Install_Nova.bat again.
    echo.
    pause
    exit /b 1
)

echo Using Python: %PY_CMD%
if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo Creating isolated application environment...
    %PY_CMD% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo Failed to create virtual environment.
        pause
        exit /b 1
    )
)

echo Installing required voice, AI, and UI libraries...
"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip --quiet
"%VENV_DIR%\Scripts\python.exe" -m pip install faster-whisper sounddevice numpy pyttsx3 pillow edge-tts
if errorlevel 1 (
    echo Package installation failed. Check your internet connection and retry.
    pause
    exit /b 1
)

echo Creating Start Menu, Desktop shortcuts, and startup entry...
"%VENV_DIR%\Scripts\python.exe" "%INSTALL_DIR%\install_nova.py" --dest "%INSTALL_DIR%" --no-start
if errorlevel 1 (
    echo Failed creating shortcuts.
    pause
    exit /b 1
)

echo Opening Customize Wizard so you can set your name and assistant name...
"%VENV_DIR%\Scripts\python.exe" "%INSTALL_DIR%\assistant.py" --customize

echo Starting assistant in the background...
if exist "%VENV_DIR%\Scripts\pythonw.exe" (
    start "" /B "%VENV_DIR%\Scripts\pythonw.exe" "%INSTALL_DIR%\assistant.py" --background
) else (
    start "" /B "%VENV_DIR%\Scripts\python.exe" "%INSTALL_DIR%\assistant.py" --background
)

echo.
echo Installation complete! Find "Nova Assistant" on your Desktop and Start Menu.
echo.
pause
