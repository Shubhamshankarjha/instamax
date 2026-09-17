@echo off
setlocal
cd /d "%~dp0"
set "PY="
where py >nul 2>nul
if %errorlevel%==0 set "PY=py -3"
if not defined PY (
  where python >nul 2>nul
  if %errorlevel%==0 set "PY=python"
)
if not defined PY (
  echo Python 3 was not found.
  echo Install Python 3 from python.org and make sure it is added to PATH.
  pause
  exit /b 1
)

echo Installing/updating InstaMax dependencies...
%PY% -m pip install --upgrade -r requirements.txt
if errorlevel 1 (
  echo.
  echo Dependency installation failed. Read the message above.
  pause
  exit /b 1
)

echo.
echo Starting InstaMax server on http://127.0.0.1:8787 ...
start "InstaMax Server" /min %PY% server.py

set "READY=0"
for /l %%i in (1,1,20) do (
  powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8787/ -TimeoutSec 1).StatusCode } catch { exit 1 }" >nul 2>&1
  if not errorlevel 1 (
    set "READY=1"
    goto :open
  )
  timeout /t 1 /nobreak >nul
)

:open
if "%READY%"=="1" (
  echo Server is running.
  start "" http://127.0.0.1:8787/
) else (
  echo.
  echo Server did not start within 20 seconds.
  echo Try running this manually to see the error:
  echo %PY% server.py
  pause
  exit /b 1
)
endlocal
