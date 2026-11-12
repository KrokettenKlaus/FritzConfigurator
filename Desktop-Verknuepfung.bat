@echo off
rem Legt auf dem Desktop eine Verknuepfung "FritzConfigurator" mit eigenem Symbol an.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\create_shortcut.ps1"
if %errorlevel% neq 0 (
    echo.
    echo Verknuepfung konnte nicht erstellt werden.
)
pause
