@echo off
setlocal
cd /d "%~dp0"

echo == DiskFucker3000: Windows setup ==

where py >nul 2>nul
if errorlevel 1 (
    echo Erro: Python Launcher ^(py^) nao encontrado.
    echo Instale Python 3 pelo site https://www.python.org/downloads/
    echo Marque a opcao para adicionar Python ao PATH.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 goto :error
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :error

".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo.
echo Instalacao concluida.
echo Para executar:
echo   .venv\Scripts\python.exe DiskFucker3000.py
echo.
pause
exit /b 0

:error
echo.
echo A instalacao falhou. Confira se o Python 3 esta instalado corretamente.
pause
exit /b 1
