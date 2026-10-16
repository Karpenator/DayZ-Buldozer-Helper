@echo off
REM Builds DayZ_Buldozer_Helper.exe from this folder (Windows, Python 3.10+).
REM Output: dist\DayZ_Buldozer_Helper.exe
REM Logs:   tests_log.txt (tests), build_log.txt (PyInstaller) - send them if something fails.
REM Usage: double-click build_exe.bat or run it from a terminal.
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (set "PY=python") else (set "PY=py -3")

REM The old exe must not be running: Windows does not allow overwriting a running exe.
tasklist /FI "IMAGENAME eq DayZ_Buldozer_Helper.exe" 2>nul | find /I "DayZ_Buldozer_Helper.exe" >nul
if not errorlevel 1 (
    echo.
    echo DayZ_Buldozer_Helper.exe is running. Close it ^(and its window over Buldozer^) and run this file again.
    goto :fail
)

if not exist .venv-build (
    echo [1/4] Creating build virtual environment...
    %PY% -m venv .venv-build
    if errorlevel 1 goto :fail
)
call .venv-build\Scripts\activate.bat
if errorlevel 1 goto :fail

echo [2/4] Installing dependencies...
python -m pip install --upgrade pip
if errorlevel 1 goto :fail
python -m pip install --upgrade -r requirements.txt pyinstaller
if errorlevel 1 goto :fail

echo [3/4] Running tests ^(log: tests_log.txt^)...
python -m pytest -q -p no:cacheprovider tests > tests_log.txt 2>&1
if errorlevel 1 (
    echo.
    echo WARNING: some tests failed. The exe is built anyway. Last lines of tests_log.txt:
    powershell -NoProfile -Command "Get-Content -Encoding UTF8 tests_log.txt -Tail 25"
    echo.
) else (
    powershell -NoProfile -Command "Get-Content -Encoding UTF8 tests_log.txt -Tail 1"
)

echo [4/4] Building exe ^(log: build_log.txt, takes a minute or two^)...
python -m PyInstaller --noconfirm --clean --onefile --windowed --name DayZ_Buldozer_Helper run_editor.py > build_log.txt 2>&1
if errorlevel 1 (
    echo.
    echo PyInstaller failed. Last lines of build_log.txt:
    powershell -NoProfile -Command "Get-Content -Encoding UTF8 build_log.txt -Tail 30"
    goto :fail
)

echo.
echo Done: %cd%\dist\DayZ_Buldozer_Helper.exe
endlocal
pause
exit /b 0

:fail
echo.
echo BUILD FAILED. See the messages above ^(and tests_log.txt / build_log.txt^).
endlocal
pause
exit /b 1
