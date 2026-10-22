@echo off
echo ============================================================
echo   DepthWizard - one-time setup
echo ============================================================
echo.

where python >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Python was not found. Install Python 3.10-3.12 and tick "Add Python to PATH".
    pause
    exit /b 1
)

echo [1/3] Creating virtual environment (.venv)...
python -m venv .venv || (echo [ERROR] Could not create .venv & pause & exit /b 1)
.\.venv\Scripts\python.exe -m pip install --upgrade pip

where nvidia-smi >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    echo [2/3] NVIDIA GPU found - installing PyTorch with CUDA 12.1...
    .\.venv\Scripts\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
) else (
    echo [2/3] No NVIDIA GPU found - installing CPU PyTorch...
    .\.venv\Scripts\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
)

echo [3/3] Installing DepthWizard dependencies...
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

echo.
echo Setup complete. Optional, for the free AI copilot:
echo   1. Install Ollama from https://ollama.com
echo   2. Run:  ollama pull qwen2.5:3b
echo Then double-click start_server.bat
pause
