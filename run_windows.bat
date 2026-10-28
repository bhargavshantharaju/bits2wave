@echo off
title NexAlert v3.0 - Local Demo
color 0A

echo.
echo  ===========================================
echo   NexAlert v3.0  -  Windows Local Demo
echo  ===========================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERROR] Python not found!
    echo  Download from: https://www.python.org/downloads/
    echo  Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

:: Create venv if needed
if not exist "venv\" (
    echo  [1/3] Creating virtual environment...
    python -m venv venv
)

:: Install deps
echo  [2/3] Installing dependencies...
call venv\Scripts\pip install flask flask-socketio flask-cors python-socketio python-engineio --quiet

:: Create dirs
if not exist "database\" mkdir database
if not exist "logs\" mkdir logs

:: Run
echo  [3/3] Starting NexAlert server...
echo.
echo  -----------------------------------------------
echo   Phone UI  :  http://localhost:5000
echo   Dashboard :  http://localhost:5000/dashboard
echo  -----------------------------------------------
echo.
echo  Press Ctrl+C to stop.
echo.

cd backend
..\venv\Scripts\python app.py
pause
