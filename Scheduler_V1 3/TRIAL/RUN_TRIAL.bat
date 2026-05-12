@echo off
echo ============================================================
echo  Scheduler Lab
echo ============================================================
echo.

cd /d "%~dp0"

set "PYTHON_CMD="
set "PYTHON_ARG="
if exist "%~dp0..\python_64bit\python.exe" (
    set "PYTHON_CMD=%~dp0..\python_64bit\python.exe"
) else if exist "%~dp0..\python\python.exe" (
    set "PYTHON_CMD=%~dp0..\python\python.exe"
) else (
    where py >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_CMD=py"
        set "PYTHON_ARG=-3"
    ) else (
        where python >nul 2>nul
        if not errorlevel 1 (
            set "PYTHON_CMD=python"
        )
    )
)

if not defined PYTHON_CMD (
    echo ERROR: No Python interpreter found.
    echo        Install Python or place it in python_64bit\python.exe.
    pause
    exit /b 1
)

echo Starting trial server...
echo.
echo Open your browser at: http://localhost:5001
echo Press Ctrl+C to stop the server.
echo.

start "" timeout /t 2 >nul && start "" "http://localhost:5001"

:restart
if defined PYTHON_ARG (
    "%PYTHON_CMD%" %PYTHON_ARG% run.py
) else (
    "%PYTHON_CMD%" run.py
)
if errorlevel 3 goto restart

pause
