@echo off
rem Launcher for pjsk-wallpaper.
rem Prefer a portable pythonw in runtime\python, then pyw / py / pythonw on PATH.
setlocal
set "SCRIPT=%~dp0wallpaper.pyw"

if exist "%~dp0runtime\python\pythonw.exe" (
    start "" "%~dp0runtime\python\pythonw.exe" "%SCRIPT%"
    exit /b 0
)

where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw -3 "%SCRIPT%"
    exit /b 0
)

where py >nul 2>nul
if %errorlevel%==0 (
    start "" py -3 "%SCRIPT%"
    exit /b 0
)

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%SCRIPT%"
    exit /b 0
)

echo Python not found. Please install Python 3.10+ and check "Add to PATH",
echo or place a portable Python in runtime\python\.
pause
