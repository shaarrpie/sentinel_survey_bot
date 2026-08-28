@echo off
echo ===================================================
echo     SentinelCore - Stealth Chrome Launcher
echo ===================================================
echo.
echo [*] Note: All existing Chrome windows must be closed 
echo     before we can bind the debugging port.
echo.
pause
echo [*] Closing Chrome...
taskkill /F /IM chrome.exe /T >nul 2>&1
timeout /t 2 /nobreak >nul

echo [*] Launching Chrome with Remote Debugging on port 9222...
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
echo [+] Success! Chrome is now ready. 
echo [+] You can now run bot.py and it will automatically attach to this browser!
echo.
pause
