@echo off
setlocal
cd /d "%~dp0"

echo [1/3] Checking environment...
if not exist "QQAnalysisApp\backend\venv" (
    echo Error: Python virtual environment not found in QQAnalysisApp\backend\venv.
    echo Please make sure you have initialized the project correctly.
    pause
    exit /b
)

echo [2/3] Starting Backend Server...
cd "QQAnalysisApp\backend"
start "QQAnalysisApp Backend" cmd /k "venv\Scripts\python main.py"

echo [3/3] Opening Web Interface...
timeout /t 3 >nul
start http://localhost:8000/static/index.html

echo.
echo ========================================================
echo App launched!
echo If the browser did not open, visit: http://localhost:8000/static/index.html
echo To run NapCat, click "Start NapCat" in the web interface.
echo ========================================================
echo.
pause
