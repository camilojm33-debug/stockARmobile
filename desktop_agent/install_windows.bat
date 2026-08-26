@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if errorlevel 1 (
  echo Python no esta instalado o no esta en PATH.
  echo Instala Python 3.11+ y vuelve a ejecutar este archivo.
  pause
  exit /b 1
)
py -m pip install -r requirements.txt
if errorlevel 1 (
  echo No se pudieron instalar las dependencias.
  pause
  exit /b 1
)
echo.
echo Agente de impresion StockArMobile listo.
echo Ejecutando en http://127.0.0.1:8765
py local_print_agent.py
