@echo off
rem Selbsttest gegen eine simulierte FRITZ!Box (Port 80 und 49000 auf 127.0.0.x muessen frei sein).
cd /d "%~dp0"
py -3 -m unittest tests.test_flow -v
pause
