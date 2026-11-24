@echo off
setlocal
cd /d "%~dp0"
set PORT=8136
set PAGE=do400_peq_webhid.html

rem Local loopback server + Windows HID bridge (zero external dependencies).

where python >nul 2>nul
if errorlevel 1 goto trypy
set PY=python
goto serve

:trypy
where py >nul 2>nul
if errorlevel 1 goto nopython
set PY=py -3

:serve
set DO400_PEQ_PORT=%PORT%
start "" "http://localhost:%PORT%/%PAGE%"
%PY% "%~dp0peq_server.py"
goto end

:nopython
echo Python 3 was not found on PATH.
echo Install Python or open %PAGE% directly (demo mode works from file://).
pause
exit /b 1

:end
endlocal
