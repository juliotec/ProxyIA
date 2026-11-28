@echo off
title Servidor IA ROCm - Qwen3.8-27B + Anti-Overflow Proxy (RX 9070 XT)

set "BAT_DIR=%~dp0"
set "LLAMA_DIR=C:\Julio\Binarios\llama-cpp"
set "MODEL_PATH=C:\Julio\Desarrollo\Modelos\Qwen3.8-27B-GGUF\Qwen3.8-27B-UD-Q3_K_XL.gguf"
set "PYTHON_EXE=C:\Julio\Agente\Tools\Python\python.exe"
set "PROXY_SCRIPT=%BAT_DIR%proxy_ctx.py"
set "LOG_FILE=%BAT_DIR%llama_server.log"

if not exist "%LLAMA_DIR%\llama-server.exe" (
    echo [ERROR] No se encontro llama-server.exe en: %LLAMA_DIR%
    pause
    exit /b 1
)

if not exist "%MODEL_PATH%" (
    echo [ERROR] No se encontro el modelo en: %MODEL_PATH%
    pause
    exit /b 1
)

if not exist "%PYTHON_EXE%" (
    echo [ERROR] No se encontro python.exe en: %PYTHON_EXE%
    pause
    exit /b 1
)

if not exist "%PROXY_SCRIPT%" (
    echo [ERROR] No se encontro proxy_ctx.py en: %BAT_DIR%
    pause
    exit /b 1
)

del /q "%LOG_FILE%" 2>nul

echo Iniciando Qwen3.8-27B en RX 9070 XT (ROCm) - Puerto interno 1235...
echo Carpeta BAT : %BAT_DIR%
echo Binario     : %LLAMA_DIR%
echo Modelo      : %MODEL_PATH%
echo Endpoint    : http://localhost:1234/v1 (Proxy 61K->48K + /fast + Reasoning 4096)
echo Log         : %LOG_FILE%
echo.

start "llama-server-rocm" /D "%LLAMA_DIR%" "%LLAMA_DIR%\llama-server.exe" -m "%MODEL_PATH%" --alias qwen3.8-27b -t 8 -ngl 99 -c 65536 -b 2048 -ub 1024 -fa on -ctk q8_0 -ctv q4_0 -np 1 --spec-type draft-mtp --spec-draft-n-max 6 --temp 0.6 --top-p 0.95 --top-k 20 --min-p 0.05 --presence-penalty 0.2 --repeat-penalty 1.05 --jinja --reasoning-format deepseek --reasoning-budget 4096 --reasoning-preserve --cache-ram 8192 --log-file "%LOG_FILE%" --port 1235

timeout /t 4 /nobreak >nul

echo Proxy activo en http://localhost:1234/v1. Presiona Ctrl + C para detener todo y liberar VRAM.
"%PYTHON_EXE%" "%PROXY_SCRIPT%"

taskkill /FI "WINDOWTITLE eq llama-server-rocm*" /F >nul 2>&1
echo.
echo Servidor y proxy detenidos. VRAM liberada.
pause
