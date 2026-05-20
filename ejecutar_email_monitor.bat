@echo off
title Monitor de Correos Electronicos
cd /d "%~dp0"
echo.
echo ============================================
echo   MONITOR DE CORREOS ELECTRONICOS
echo   Leyendo 9 buzones...
echo ============================================
echo.
C:\Users\DLP\AppData\Local\Microsoft\WindowsApps\py.exe -X utf8 email_monitor.py
echo.
echo ============================================
echo   PROCESO TERMINADO
echo ============================================
echo.
pause
