@echo off
chcp 65001 >nul
title Sentinel Launcher
echo ============================================
echo   Sentinel Survey Bot - All-in-One Launcher
echo ============================================
echo.

cd /d "%~dp0"

echo [1/3] Starting Python backend...
start "Sentinel Backend" cmd /c "python backend.py"

echo [2/3] Starting OmniRoute proxy...
echo     (first run may take 60+ seconds to initialize)
start "Sentinel OmniRoute" cmd /c "npx omniroute"

echo [3/3] Waiting for services to be ready...
call :wait_for http://127.0.0.1:8000/status 30 Backend
call :wait_for http://localhost:20128/api/monitoring/health 90 OmniRoute

echo.
echo [+] Backend:  http://127.0.0.1:8000
echo [+] OmniRoute: http://localhost:20128/v1
echo.

rem The old launcher ran bare "start chrome" — no extension loaded, and
rem "close this window to stop" killed nothing. Now Chrome gets a dedicated
rem profile + the extension, and both child processes are reaped on exit.
set "CHROME_PROFILE=%TEMP%\sentinel_chrome_profile"
echo Opening Chrome with the Sentinel extension (dedicated profile)...
start "Sentinel Chrome" chrome --user-data-dir="%CHROME_PROFILE%" --load-extension="%~dp0extension" --new-window

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
goto wait_loop
