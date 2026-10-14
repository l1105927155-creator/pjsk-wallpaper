@echo off
rem Installer launcher for pjsk-wallpaper.
rem Prefer the pyw launcher, then py, then pythonw on PATH.
rem If none found, Python is not installed - show instructions and wait.
setlocal
set "SCRIPT=%~dp0installer.pyw"

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

echo Python not found.
echo.
echo Please install Python 3.10 or newer first:
echo   https://www.python.org/downloads/
echo Be sure to check "Add python.exe to PATH" during setup,
echo then run this installer again.
pause
