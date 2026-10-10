@echo off
setlocal
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\project-switch.ps1" %*
set "EM_SWITCH_EXIT=%ERRORLEVEL%"
if not "%EM_SWITCH_EXIT%"=="0" pause
exit /b %EM_SWITCH_EXIT%
