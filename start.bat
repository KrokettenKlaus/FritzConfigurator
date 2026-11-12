@echo off
rem Startet FritzConfigurator. Benoetigt Python 3.8+ (python.org, mit tkinter - Standard).
cd /d "%~dp0"

if exist "FritzConfigurator.exe" (
    start "" "FritzConfigurator.exe"
    exit /b 0
)

where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw -3 "FritzConfigurator.pyw"
    exit /b 0
)
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "FritzConfigurator.pyw"
    exit /b 0
)

echo Python wurde nicht gefunden.
echo.
echo Installation (einmalig), z.B. per:
echo     winget install -e --id Python.Python.3.12
echo oder von https://www.python.org/downloads/ (Haken bei "Add python.exe to PATH" setzen).
echo.
pause
exit /b 1
