@echo off
chcp 65001 >nul
setlocal
title BatchCut Studio
cd /d "%~dp0"

echo ==========================================
echo   BatchCut Studio - 本地剪辑标注工场
echo ==========================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 没找到 python，请先安装 Python 3.10+ 并加入 PATH
  pause
  exit /b 1
)

rem 服务自己会挑可用端口并自动打开浏览器；关掉这个窗口即停止服务
python server.py %*
set EXITCODE=%errorlevel%

echo.
if not "%EXITCODE%"=="0" (
  echo [服务异常退出，代码 %EXITCODE%]
  echo 常见原因：端口全被占用 / ffmpeg 路径不正确
) else (
  echo 服务已正常退出。
)
pause >nul
