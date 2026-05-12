@echo off
echo ============================================================
echo  Local Production Planner
echo ============================================================
echo.

REM Change to the directory where this batch file lives
cd /d "%~dp0"

REM Prefer a bundled 64-bit interpreter if present, otherwise fall back to system Python.
set "PYTHON_CMD="
set "PYTHON_ARG="
if exist "%~dp0python_64bit\python.exe" (
    set "PYTHON_CMD=%~dp0python_64bit\python.exe"
) else if exist "%~dp0python\python.exe" (
    set "PYTHON_CMD=%~dp0python\python.exe"
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
    echo        Install Python 64-bit or place it in python_64bit\python.exe.
    pause
    exit /b 1
)

echo Starting server...
echo.
echo Open your browser at: http://localhost:5000
echo Press Ctrl+C to stop the server.
echo.

REM Open browser after short delay (start in background)
start "" timeout /t 2 >nul && start "" "http://localhost:5000"

:restart
if defined PYTHON_ARG (
    "%PYTHON_CMD%" %PYTHON_ARG% app.py
) else (
    "%PYTHON_CMD%" app.py
)
if errorlevel 3 (
    echo.
    echo Change detected. Restarting server...
    timeout /t 2 /nobreak >nul
    goto restart
)

pause
