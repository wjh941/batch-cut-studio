@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem 读取服务实际地址（端口被系统圈占时服务会自动顺延，running.json 里是真实 URL）
if not exist "running.json" goto :noserver

for /f "usebackq delims=" %%U in (`python -c "import json;print(json.load(open('running.json',encoding='utf-8'))['url'])" 2^>nul`) do set "URL=%%U"
if "%URL%"=="" goto :noserver

echo 打开 %URL%
start "" "%URL%"
exit /b 0

:noserver
echo 服务没在运行，正在启动……
echo.
start "" "%~dp0启动.bat"
