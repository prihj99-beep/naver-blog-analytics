@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python virtual environment is missing. See keyword_analysis\README.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -B -m keyword_analysis %*
if errorlevel 1 (
  echo Keyword analysis could not start. See the message above.
  pause
  exit /b 1
)
exit /b 0
