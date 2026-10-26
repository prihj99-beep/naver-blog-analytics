@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python virtual environment is missing. See keyword_analysis\README.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -B -m keyword_analysis.secure_launch %*
if errorlevel 1 (
  echo Analysis did not start or was cancelled. No credentials were saved.
  pause
  exit /b 1
)
exit /b 0
