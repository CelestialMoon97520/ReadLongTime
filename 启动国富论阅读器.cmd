@echo off
chcp 65001 >nul
set PYTHONUTF8=1
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 (
  py -3 "%~dp0reader_server.py"
) else (
  python "%~dp0reader_server.py"
)
if errorlevel 1 (
  echo.
  echo Service failed. Check that Python 3 is installed.
  pause
)
