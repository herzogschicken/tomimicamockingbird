@echo off
REM Start tomimicamockingbird on Windows. Double-click this file.
REM The first run sets everything up (takes a few minutes).
setlocal
cd /d "%~dp0"
if "%PORT%"=="" set PORT=8765
if "%HOST%"=="" set HOST=127.0.0.1

REM 1. Find Python 3.10 or newer
set PY=
for %%V in (3.12 3.13 3.11 3.10) do (
  if not defined PY ( py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V" )
)
if not defined PY ( python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1 && set "PY=python" )
if not defined PY (
  echo.
  echo Python 3.10 or newer was not found. See "Step 1" for Windows in README.md.
  echo.
  pause
  exit /b 1
)

REM 2. Make a private Python folder (.venv) for the app, once
if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up. This takes a few minutes...
  %PY% -m venv .venv
  if errorlevel 1 ( echo Could not create the .venv folder. & pause & exit /b 1 )
)

REM 3. Install packages (only when something changed)
set WANT_GPU=0
if not "%TMM_NO_GPU%"=="1" ( where nvidia-smi >nul 2>&1 && set WANT_GPU=1 )
set STAMP=v3-gpu%WANT_GPU%
set DONE=
if exist ".venv\installed.txt" set /p DONE=<".venv\installed.txt"
if not "%DONE%"=="%STAMP%" (
  echo Installing packages...
  ".venv\Scripts\python.exe" -m pip install -q --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
  if errorlevel 1 ( echo Installing packages failed. Check your internet and try again. & pause & exit /b 1 )
  if "%WANT_GPU%"=="1" (
    echo NVIDIA card found: adding GPU support, about 600 MB, one time only...
    ".venv\Scripts\python.exe" -m pip install -q -r requirements-gpu.txt
  ) else (
    echo No NVIDIA card found: transcription will use the CPU.
  )
  > ".venv\installed.txt" echo %STAMP%
)

REM 4. Check the helpers
where ffprobe >nul 2>&1 || echo Warning: ffmpeg is not installed. See "Step 1" for Windows in README.md.

echo.
echo tomimicamockingbird is running. Open this in your browser:  http://127.0.0.1:%PORT%
echo To stop it, close this window.
echo.
start "" "http://127.0.0.1:%PORT%"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host %HOST% --port %PORT%
pause
