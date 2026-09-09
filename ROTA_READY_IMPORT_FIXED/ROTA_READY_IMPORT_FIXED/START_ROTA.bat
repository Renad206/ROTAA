@echo off
cd /d "%~dp0"
title ROTA
if not exist ".venv\Scripts\python.exe" (
  echo [ROTA] First setup...
  python -m venv .venv
  if errorlevel 1 goto :fail
)
if not exist ".rota_ready" (
  echo [ROTA] Installing requirements. You only do this once...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto :fail
  echo ready> .rota_ready
)
echo [ROTA] Starting...
".venv\Scripts\python.exe" start_rota.py
exit /b
:fail
echo.
echo ROTA could not start. Copy the error above and send it to ChatGPT.
pause
