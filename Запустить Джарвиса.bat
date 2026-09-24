@echo off
chcp 65001 >nul
title Jarvis Voice Assistant
echo ============================================
echo   JARVIS - голосовой ассистент
echo   Скажите "Джарвис" для активации
echo ============================================
python "%~dp0jarvis_bridge.py"
pause
