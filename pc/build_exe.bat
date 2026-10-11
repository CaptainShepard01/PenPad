@echo off
rem Builds dist\PenPad.exe: the Windows app (penpad_gui.py) as one file, no Python needed to run it.
rem Needs uv; PyInstaller is fetched by uvx and not installed anywhere.
setlocal
cd /d "%~dp0"

where uv >nul 2>&1 || (
    echo uv not found. See README.md, step 1.
    exit /b 1
)

rem adb goes inside the exe, so PenPad.exe works on PCs without the Android SDK.
set "PT=%ANDROID_HOME%\platform-tools"
if "%ANDROID_HOME%"=="" set "PT=C:\Android\platform-tools"
if not exist "%PT%\adb.exe" (
    echo adb.exe not found in %PT%. See README.md, step 3.
    exit /b 1
)

uvx pyinstaller --noconfirm --clean --onefile --windowed --name PenPad ^
    --icon "%~dp0penpad.ico" --add-data "%~dp0penpad.ico;." ^
    --add-binary "%PT%\adb.exe;adb" --add-binary "%PT%\AdbWinApi.dll;adb" ^
    --add-binary "%PT%\AdbWinUsbApi.dll;adb" --add-data "%PT%\NOTICE.txt;adb" ^
    --distpath "%~dp0dist" --workpath "%~dp0build" --specpath "%~dp0build" ^
    penpad_gui.py
if errorlevel 1 (
    echo.
    echo BUILD FAILED
    exit /b 1
)
echo.
echo Built %~dp0dist\PenPad.exe
