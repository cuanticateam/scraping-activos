@echo off
title Actualizando Inmuebles Medellin...
cd /d "%~dp0"
echo.
echo ============================================
echo   ACTUALIZANDO TABLA DE INMUEBLES MEDELLIN
echo ============================================
echo.
C:\Users\DLP\AppData\Local\Microsoft\WindowsApps\py.exe -X utf8 scraping_activos.py
echo.
echo ============================================
echo   PROCESO TERMINADO
echo ============================================
echo.
pause
