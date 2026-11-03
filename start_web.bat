@echo off
cd /d "%~dp0"
echo.
echo   Order panel starting...
echo   URL: http://127.0.0.1:8080
echo.
echo   Keep this window open. Close it to stop the panel.
echo.

rem Open the browser a few seconds later, after the server is listening.
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8080"

python web\app.py --port 8080

echo.
echo   Panel stopped.
pause
