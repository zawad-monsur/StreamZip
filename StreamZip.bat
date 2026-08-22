@echo off
rem Launch StreamZip with no console window. Falls back to python.exe if
rem pythonw is missing (then you get a console alongside the app).
setlocal
cd /d "%~dp0"
where pythonw >nul 2>nul && (start "" pythonw app.py & exit /b)
where python  >nul 2>nul && (python app.py & exit /b)
echo Python was not found on PATH. Install it from https://python.org
pause
