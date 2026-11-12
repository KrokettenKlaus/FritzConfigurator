@echo off
rem Baut eine eigenstaendige FritzConfigurator.exe (danach ist auf anderen PCs kein Python noetig).
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel% neq 0 (
    echo Python-Launcher "py" nicht gefunden - bitte Python von python.org installieren.
    pause
    exit /b 1
)

py -3 -m pip install --upgrade pyinstaller || goto :fail
if not exist "%~dp0FritzConfigurator.ico" py -3 "%~dp0tools\make_icon.py"
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name FritzConfigurator ^
    --icon "%~dp0FritzConfigurator.ico" --add-data "%~dp0FritzConfigurator.ico;." ^
    --distpath "%~dp0" --workpath "%TEMP%\fritzconf_build" --specpath "%TEMP%\fritzconf_build" ^
    "FritzConfigurator.pyw" || goto :fail

echo.
echo Fertig: %~dp0FritzConfigurator.exe
echo (Ordner "firmware" und "logs" werden neben der EXE angelegt.)
pause
exit /b 0

:fail
echo.
echo Build fehlgeschlagen.
pause
exit /b 1
