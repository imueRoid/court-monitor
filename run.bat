@echo off
setlocal
cd /d "%~dp0"
if exist "dist\court-search.exe" (
  "dist\court-search.exe"
  exit /b
)
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" app.py
) else (
  python app.py
)
if errorlevel 1 pause
