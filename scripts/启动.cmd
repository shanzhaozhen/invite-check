@echo off
cd /d "%~dp0.."
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
title 邀请站点工具 InviteTool

%PY% -c "import playwright, cloakbrowser" >nul 2>nul
if errorlevel 1 (
    echo 第一次运行或依赖有更新，正在安装依赖，请稍等...
    %PY% -m pip install -r requirements.txt
    %PY% -c "import playwright" >nul 2>nul
    if errorlevel 1 (
        echo.
        echo 依赖安装失败：请确认装了 Python 3.10 以上，并且网络可用。
        pause
        exit /b 1
    )
)

%PY% gui.py
if errorlevel 1 (
    echo.
    echo 程序异常退出，上面是错误信息。
    pause
)
