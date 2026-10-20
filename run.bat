@echo off
REM Start tomimicamockingbird on Windows. Double-click this file.
REM The first run sets everything up (takes a few minutes).
setlocal EnableExtensions
cd /d "%~dp0"
if "%PORT%"=="" set PORT=8765
if "%HOST%"=="" set HOST=127.0.0.1

REM Programs installed a moment ago (Python, FFmpeg, adb) are not visible to windows that were
REM already open. Reload the PATH from Windows settings so they are found anyway.
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')"`) do set "PATH=%%P;%PATH%"
set "PATH=%LOCALAPPDATA%\Microsoft\WinGet\Links;%PATH%"

REM 1. Find Python 3.10 or newer (the "python" from the Microsoft Store does not count)
set "PYEXE="
if exist ".venv\Scripts\python.exe" goto venv_ready
for %%V in (3.12 3.13 3.11 3.10 3.14) do call :trypy py -%%V
call :trypy python
call :trypy python3
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do call :trypy "%%~fD\python.exe"
for /d %%D in ("%ProgramFiles%\Python3*") do call :trypy "%%~fD\python.exe"
for /d %%D in ("%SystemDrive%\Python3*") do call :trypy "%%~fD\python.exe"
if not defined PYEXE goto no_python
echo Using Python: %PYEXE%

REM 2. Make a private Python folder (.venv) for the app, once
echo First run: setting up. This takes a few minutes...
"%PYEXE%" -m venv .venv
if errorlevel 1 goto venv_failed

:venv_ready
REM 3. Install packages (only when something changed)
set WANT_GPU=0
if not "%TMM_NO_GPU%"=="1" ( where nvidia-smi >nul 2>&1 && set WANT_GPU=1 )
set STAMP=v4-gpu%WANT_GPU%
set DONE=
if exist ".venv\installed.txt" set /p DONE=<".venv\installed.txt"
if "%DONE%"=="%STAMP%" goto start_app
echo Installing packages...
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 goto pip_failed
if "%WANT_GPU%"=="1" goto install_gpu
echo No NVIDIA card found: transcription will use the CPU.
goto mark_done
:install_gpu
echo NVIDIA card found: adding GPU support, about 600 MB, one time only...
".venv\Scripts\python.exe" -m pip install -q -r requirements-gpu.txt
:mark_done
> ".venv\installed.txt" echo %STAMP%

:start_app
REM 4. Check the helpers
where ffprobe >nul 2>&1 || echo Warning: FFmpeg was not found. See "Step 1" for Windows in README.md.
echo.
echo tomimicamockingbird is running. Open this in your browser:  http://127.0.0.1:%PORT%
echo To stop it, close this window.
echo.
start "" "http://127.0.0.1:%PORT%"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host %HOST% --port %PORT%
pause
exit /b 0

:trypy
REM Try one way of running Python. Keep the first one that is 3.10 or newer.
if defined PYEXE goto :eof
for /f "usebackq delims=" %%X in (`%* tools\pyinfo.py 2^>nul`) do if not defined PYEXE set "PYEXE=%%X"
goto :eof

:no_python
echo.
echo Python 3.10 or newer was not found on this computer.
echo.
echo ---- What this computer reports (send this if you ask for help) ----
echo [where py]
where py 2>&1
echo [where python]
where python 2>&1
echo [py --list]
py --list 2>&1
echo [py -3.12 tools\pyinfo.py]
py -3.12 tools\pyinfo.py 2>&1
echo ------------------------------------------------------------------
echo.
echo Fix: open the Terminal and run:   winget install -e --id Python.Python.3.12
echo Then sign out of Windows and back in (or restart), and double-click run.bat again.
echo.
pause
exit /b 1

:venv_failed
echo.
echo Could not create the .venv folder with %PYEXE%.
echo Try moving the app folder to Documents and run it again.
echo.
pause
exit /b 1

:pip_failed
echo.
echo Installing packages failed. Check the internet connection and run run.bat again.
echo.
pause
exit /b 1
