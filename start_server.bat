@echo off
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Run setup_laptop.bat first.
    pause
    exit /b 1
)
where nvidia-smi >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    rem A 6 GB GPU runs the Base model comfortably next to a 3B copilot model.
    if not defined DW_MODEL_SIZE set DW_MODEL_SIZE=base
)
echo Starting DepthWizard on http://localhost:8000  (phones: use the address printed below)
start http://localhost:8000
.\.venv\Scripts\python.exe -m api.main
