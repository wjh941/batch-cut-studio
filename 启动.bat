@echo off
chcp 65001 >nul
title BatchCut Studio
cd /d "%~dp0"
python server.py %*
echo.
echo 服务已退出。按任意键关闭窗口……
pause >nul
