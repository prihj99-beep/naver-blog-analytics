@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Python virtual environment is missing.
  echo Please check README.md in this project folder.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import playwright" >nul 2>&1
if errorlevel 1 (
  echo Playwright is missing. Please check requirements.txt.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m analytics.dashboard
exit /b 0
