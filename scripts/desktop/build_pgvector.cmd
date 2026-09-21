@echo off
call "%~1\VC\Auxiliary\Build\vcvars64.bat"
if errorlevel 1 exit /b %errorlevel%
set "PGROOT=%~2"
nmake /F Makefile.win
if errorlevel 1 exit /b %errorlevel%
nmake /F Makefile.win install
exit /b %errorlevel%
