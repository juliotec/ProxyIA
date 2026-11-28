@echo off
title OpenCode - Longshot (Local ROCm)

set "OPENCODE_DIR=C:\Julio\Binarios\opencode"
set "WORKSPACE_DIR=C:\Julio\Agente\Longshot"

if not exist "%OPENCODE_DIR%\opencode.exe" (
    echo [ERROR] No se encontro opencode.exe en: %OPENCODE_DIR%
    pause
    exit /b 1
)

if not exist "%WORKSPACE_DIR%" (
    echo [ERROR] No se encontro el workspace en: %WORKSPACE_DIR%
    pause
    exit /b 1
)

set "PATH=%OPENCODE_DIR%;%PATH%"
cd /d "%WORKSPACE_DIR%"

echo Iniciando OpenCode...
echo Binario   : %OPENCODE_DIR%\opencode.exe
echo Workspace : %WORKSPACE_DIR%
echo.

"%OPENCODE_DIR%\opencode.exe"
