@echo off
REM Builds "Banjo Memory Viewer.exe" - a standalone Windows executable.
REM Run this once on a machine with Python installed. The resulting
REM .exe in dist\ can then be shared and run without Python.

setlocal

REM Pinned to the confirmed native Windows Python install.
REM (Plain "python" on this machine resolves to something else - a WSL/Linux
REM  interpreter - which is why earlier attempts installed to /usr/lib/...)
set PY="C:\Users\X\AppData\Local\Programs\Python\Python312\python.exe"

if not exist %PY% (
    echo Could not find Python at %PY%.
    echo Edit build_exe.bat and update the PY path, or install Python from https://python.org
    pause
    exit /b 1
)

echo Using: %PY%
%PY% -c "import sys; print(sys.executable, sys.platform)"

echo Installing/upgrading build dependencies...
%PY% -m pip install --upgrade pip setuptools wheel
%PY% -m pip install --only-binary=:all: pywin32 pyinstaller
if errorlevel 1 (
    echo.
    echo Could not find prebuilt wheels for pywin32/pyinstaller for this Python.
    echo Try a 64-bit Python from python.org ^(3.10-3.12 recommended^) and re-run.
    pause
    exit /b 1
)

echo Building "Banjo Memory Viewer.exe"...
%PY% -m PyInstaller --onefile --windowed --name "Banjo Memory Viewer" --add-data "enums.h;." main.py

if exist "dist\Banjo Memory Viewer.exe" (
    REM Watches files are read/write user data (edited via the SAVE button),
    REM so they're copied next to the exe rather than baked in - that way
    REM edits persist across runs instead of vanishing with a temp extract
    REM dir. The app looks for them in a "watches" subfolder next to the
    REM exe, so that's where they need to land. Only copied if missing, so
    REM a rebuild won't clobber a dist\watches\ copy the user has since
    REM customized.
    if not exist dist\watches mkdir dist\watches
    for %%F in (bk_watches.json bt_watches.json bt_xenia_watches.json bk_xenia_watches.json) do (
        if exist watches\%%F if not exist dist\watches\%%F copy /y watches\%%F dist\watches\%%F >nul
    )
    echo.
    echo Build succeeded: dist\Banjo Memory Viewer.exe
    echo Share the whole dist\ folder ^(exe + the watches\ subfolder^) -
    echo the exe alone still runs, but per-game watch lists need those
    echo json files sitting in a "watches" folder right next to it.
) else (
    echo.
    echo Build failed - see output above.
)

pause
