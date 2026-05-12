@echo off
echo ============================================================
echo  Local Production Planner - Install Dependencies
echo ============================================================
echo.

REM Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found.
    echo Please install Python 3.8+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

echo Python found:
python --version
echo.

echo Installing required packages...
python -m pip install --upgrade pip
python -m pip install flask>=3.0.0

if errorlevel 1 (
    echo.
    echo ERROR: Failed to install packages.
    echo Try running this as Administrator, or manually run:
    echo   pip install flask
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Installation complete!
echo  Run RUN_APP.bat to start the application.
echo ============================================================
pause
