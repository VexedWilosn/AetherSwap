@echo off
setlocal

set "ROOT=%~dp0"
cd /d "%ROOT%" || exit /b 1

set "VENV_DIR=%ROOT%.venv"
set "ACTIVATE=%VENV_DIR%\Scripts\activate.bat"
set "NEED_INSTALL=0"

if not exist "%ACTIVATE%" (
    echo [AetherSwap] Virtual environment not found. Creating .venv ...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [AetherSwap] Failed to create virtual environment.
        echo [AetherSwap] Make sure Python is installed and available in PATH.
        pause
        exit /b 1
    )

    call "%ACTIVATE%"
    if errorlevel 1 (
        echo [AetherSwap] Failed to activate virtual environment.
        pause
        exit /b 1
    )

    set "NEED_INSTALL=1"
) else (
    call "%ACTIVATE%"
    if errorlevel 1 (
        echo [AetherSwap] Failed to activate virtual environment.
        pause
        exit /b 1
    )
)

python -c "import fastapi, sqlmodel, uvicorn" >nul 2>nul
if errorlevel 1 set "NEED_INSTALL=1"

if "%NEED_INSTALL%"=="1" (
    echo [AetherSwap] Installing dependencies ...
    python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 (
        echo [AetherSwap] Failed to install dependencies.
        pause
        exit /b 1
    )
)

echo [AetherSwap] Starting project ...
python run.py

if errorlevel 1 (
    echo [AetherSwap] Project exited with an error.
    pause
    exit /b 1
)

endlocal
