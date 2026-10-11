@echo off
rem PenPad build without Gradle or Android Studio.
rem   build.bat           -> build\PenPad.apk
rem   build.bat install   -> also installs on the USB-connected phone and sets up adb reverse
setlocal
cd /d "%~dp0"

if "%ANDROID_HOME%"=="" (
    echo ANDROID_HOME is not set. See README.md, step 2.
    exit /b 1
)

rem ---- pick the newest installed build-tools and platform ----
rem Sorted as version numbers (a plain name sort puts android-9 after android-37).
rem Previews (-rc, -beta, -ext, canary) are skipped.
set "BT="
set "PLATDIR="
for /f "delims=" %%d in ('powershell -NoProfile -Command "Get-ChildItem -Directory '%ANDROID_HOME%\build-tools' -ErrorAction SilentlyContinue | Where-Object Name -match '^\d+(\.\d+)+$' | Sort-Object { [version]$_.Name } | Select-Object -Last 1 -ExpandProperty Name"') do set "BT=%ANDROID_HOME%\build-tools\%%d"
for /f "delims=" %%d in ('powershell -NoProfile -Command "Get-ChildItem -Directory '%ANDROID_HOME%\platforms' -ErrorAction SilentlyContinue | Where-Object Name -match '^android-\d+(\.\d+)?$' | Sort-Object { [version](($_.Name -replace '^android-', '') + '.0') } | Select-Object -Last 1 -ExpandProperty Name"') do set "PLATDIR=%%d"
if not defined BT (
    echo No build-tools found in "%ANDROID_HOME%\build-tools". See README.md, step 3.
    exit /b 1
)
set "JAR=%ANDROID_HOME%\platforms\%PLATDIR%\android.jar"
set "API=%PLATDIR:android-=%"
for /f "tokens=1 delims=." %%a in ("%API%") do set "API=%%a"
if not exist "%JAR%" (
    echo No platform found in "%ANDROID_HOME%\platforms". See README.md, step 3.
    exit /b 1
)
echo build-tools: %BT%
echo platform:    %PLATDIR% (targetSdk %API%)

rem ---- one-time debug signing key ----
if not exist debug.keystore (
    echo Creating debug.keystore
    keytool -genkeypair -keystore debug.keystore -alias penpad -keyalg RSA -keysize 2048 ^
        -validity 10000 -storepass android -keypass android -dname "CN=PenPad"
    if errorlevel 1 goto :fail
)

if exist build rmdir /s /q build
mkdir build\classes build\dex

echo [1/7] Compiling resources (app icon)
"%BT%\aapt2.exe" compile --dir res -o build\res.zip
if errorlevel 1 goto :fail

echo [2/7] Linking manifest and resources
"%BT%\aapt2.exe" link -o build\unsigned.apk -I "%JAR%" --manifest AndroidManifest.xml ^
    --target-sdk-version %API% --version-code 1 --version-name 1.0 build\res.zip
if errorlevel 1 goto :fail

echo [3/7] Compiling Java
javac --release 11 -classpath "%JAR%" -d build\classes src\com\example\penpad\MainActivity.java
if errorlevel 1 goto :fail

echo [4/7] Converting to DEX
jar --create --file build\classes.jar -C build\classes .
if errorlevel 1 goto :fail
call "%BT%\d8.bat" --release --min-api 30 --lib "%JAR%" --output build\dex build\classes.jar
if errorlevel 1 goto :fail

echo [5/7] Adding classes.dex to the APK
rem Append-only, so entries aapt2 wrote keep their compression.
rem Uses uv's Python if uv is installed, otherwise python from PATH.
set "PY=python"
where uv >nul 2>&1 && set "PY=uv run --no-project python"
%PY% -c "import sys,zipfile; zipfile.ZipFile(sys.argv[1],'a',zipfile.ZIP_DEFLATED).write(sys.argv[2],'classes.dex')" build\unsigned.apk build\dex\classes.dex
if errorlevel 1 goto :fail

echo [6/7] Aligning
"%BT%\zipalign.exe" -f -p 4 build\unsigned.apk build\aligned.apk
if errorlevel 1 goto :fail

echo [7/7] Signing
call "%BT%\apksigner.bat" sign --ks debug.keystore --ks-pass pass:android --key-pass pass:android ^
    --out build\PenPad.apk build\aligned.apk
if errorlevel 1 goto :fail

echo.
echo Built build\PenPad.apk

if /i not "%~1"=="install" exit /b 0

echo Installing on phone
"%ANDROID_HOME%\platform-tools\adb.exe" install -r build\PenPad.apk
if errorlevel 1 goto :fail
"%ANDROID_HOME%\platform-tools\adb.exe" reverse tcp:47300 tcp:47300
if errorlevel 1 goto :fail
echo Installed. adb reverse is set up.
exit /b 0

:fail
echo.
echo BUILD FAILED
exit /b 1
