@echo off
chcp 65001 >nul
title Jarvis Voice Bridge
echo ============================================
echo   JARVIS - голосовой мост к MultiTool
echo   Скажите "Джарвис" для активации
echo   Для выхода: Ctrl+C или "Джарвис, стоп"
echo ============================================
python "%~dp0jarvis_bridge.py"
pause