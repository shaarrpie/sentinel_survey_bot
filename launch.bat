@echo off
chcp 65001 >nul
title Sentinel Launcher
echo ============================================
echo   Sentinel Survey Bot - Backend Launcher
echo ============================================
echo.

cd /d "%~dp0"

echo [1/2] Starting Python backend...
start "Sentinel Backend" cmd /c "python backend.py"

echo [2/2] Starting OmniRoute proxy...
echo     (first run may take 60+ seconds to initialize)
start "Sentinel OmniRoute" cmd /c "npx omniroute"

echo.
echo Waiting for services to be ready...
call :wait_for http://127.0.0.1:8000/status 30 Backend
call :wait_for http://localhost:20128/api/monitoring/health 90 OmniRoute

echo.
echo [+] Backend:   http://127.0.0.1:8000
echo [+] OmniRoute: http://localhost:20128/v1
echo.
echo Open Chrome and load the extension from:
echo   %~dp0extension
echo.
echo ============================================
echo   All systems launched.
echo   Closing this window stops the backend and
echo   OmniRoute (the Sentinel Chrome window stays
echo   open — close it manually).
echo ============================================
pause

:cleanup
echo [~] Stopping backend and OmniRoute...
taskkill /f /fi "WINDOWTITLE eq Sentinel Backend*" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq Sentinel OmniRoute*" >nul 2>&1
taskkill /f /im uvicorn.exe >nul 2>&1
exit /b 0

:wait_for
setlocal
set "URL=%~1"
set "TIMEOUT=%~2"
set "NAME=%~3"
set /a "ELAPSED=0"
set /a "INTERVAL=2"
:wait_loop
curl -s -o nul -w "%%{http_code}" "%URL%" | findstr /r "^[23][0-9][0-9]$" >nul 2>&1
if not errorlevel 1 (
    echo [+] %NAME% is ready.
    endlocal & exit /b 0
)
if %ELAPSED% geq %TIMEOUT% (
    echo [!] %NAME% did not respond within %TIMEOUT%s. Continuing anyway...
    endlocal & exit /b 0
)
timeout /t %INTERVAL% /nobreak >nul
set /a "ELAPSED+=INTERVAL"
goto wait_for
