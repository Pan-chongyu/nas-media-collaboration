@echo off
setlocal
set ROOT=%~dp0..
set OUT=%ROOT%build\verification\frozen-smoke.json
set DATA=%ROOT%build\verification\frozen-data
if exist "%OUT%" del /q "%OUT%"
if exist "%DATA%" rmdir /s /q "%DATA%"
"%ROOT%build\0.3.0\dist\素材协作\素材协作.exe" --data-dir "%DATA%" --no-auto-sync --smoke-test "%OUT%"
if errorlevel 1 exit /b %errorlevel%
type "%OUT%"
