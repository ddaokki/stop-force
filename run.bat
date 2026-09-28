@echo off
cd /d "%~dp0"
echo === Stop-Force ===

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Install Python 3.10+ from python.org and CHECK "Add Python to PATH".
  pause
  exit /b 1
)

if not exist ".env" (
  if exist ".env.example" (
    copy ".env.example" ".env" >nul
  ) else (
    > ".env" echo NVIDIA_API_KEY=
    >> ".env" echo NVIDIA_MODEL=nvidia/nemotron-3.5-lightning-30b-a3b
  )
  echo [INFO] Created .env - paste your key after NVIDIA_API_KEY= , save, close Notepad.
  notepad ".env"
)

if not exist ".venv\Scripts\python.exe" (
  echo [INFO] Creating virtual environment...
  python -m venv .venv
  if errorlevel 1 (
    echo [ERROR] Failed to create venv.
    pause
    exit /b 1
  )
)

echo [INFO] Installing packages...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo [ERROR] pip install failed.
  pause
  exit /b 1
)

echo [INFO] Starting web app... (browser opens at http://localhost:8501)
".venv\Scripts\python.exe" -m streamlit run app.py
pause
