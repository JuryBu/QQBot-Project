@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title BossLady Console

cd /d "%~dp0"
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"

set "PYTHON=%ROOT%\AstrBot\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo [ERROR] Python venv not found: %PYTHON%
    pause
    exit /b 1
)

echo ====================================
echo   BossLady Console - Starting...
echo ====================================
echo.
echo   Project: %ROOT%
echo   Python:  %PYTHON%
echo.

:: 1. Start NapCat
echo [1/3] Starting NapCat...
set "NAPCAT_SHELL_DIR="
for /d %%d in ("%ROOT%\NapCat*") do (
    for /d %%s in ("%%d\NapCat.*.Shell") do (
        if exist "%%s\NapCatWinBootMain.exe" (
            if exist "%%s\QQ.exe" (
                set "NAPCAT_SHELL_DIR=%%s"
            )
        )
    )
)

if not defined NAPCAT_SHELL_DIR (
    echo       [WARN] NapCat not found, skipping.
    goto :napcat_done
)

echo       Found: %NAPCAT_SHELL_DIR%
start /B "" "%NAPCAT_SHELL_DIR%\NapCatWinBootMain.exe" >nul 2>&1
echo       [OK] NapCat started.

:napcat_done
echo.
timeout /t 3 /nobreak >nul

:: 2. Start AstrBot (must run from its own dir for relative imports)
echo [2/3] Starting AstrBot...
if not exist "%ROOT%\AstrBot\main.py" (
    echo       [WARN] AstrBot not found, skipping.
    goto :astrbot_done
)

pushd "%ROOT%\AstrBot"
start /B "" "%PYTHON%" main.py >nul 2>&1
popd
echo       [OK] AstrBot started.

:astrbot_done
echo.
timeout /t 5 /nobreak >nul

:: 3. Start BossLady Console (foreground)
echo [3/3] Starting BossLady Console...
if not exist "%ROOT%\BossLady_Console\backend\main.py" (
    echo       [ERROR] Console backend not found!
    goto :fatal_error
)

start /B cmd /c "timeout /t 2 /nobreak >nul && start http://localhost:8090"

echo.
echo ============================================
echo   All services started in this window!
echo   Console:  http://localhost:8090
echo   AstrBot:  http://localhost:6185
echo   NapCat:   http://localhost:6099
echo.
echo   Close this window or press Ctrl+C to stop
echo ============================================
echo.

cd /d "%ROOT%\BossLady_Console"
"%PYTHON%" -m uvicorn backend.main:app --host 127.0.0.1 --port 8090 --no-access-log 2>&1
set EXIT_CODE=%ERRORLEVEL%

echo.
echo Stopping all services...
taskkill /f /im NapCatWinBootMain.exe >nul 2>&1
taskkill /f /im QQ.exe >nul 2>&1
echo All services stopped.
pause
endlocal
exit /b %EXIT_CODE%

:fatal_error
echo [FATAL] Startup failed!
pause
endlocal
exit /b 1
