@echo off
setlocal
python "%~dp0run_frozen_smoke.py" %*
exit /b %errorlevel%
