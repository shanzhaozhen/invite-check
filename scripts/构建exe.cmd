@echo off
cd /d "%~dp0.."
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
title 打包 exe

echo 正在安装/更新 PyInstaller...
%PY% -m pip install --quiet --upgrade pyinstaller
if errorlevel 1 goto :err

echo 正在打包，需要几分钟，别关窗口...
rem --noconsole：窗口程序，双击不会多一个黑窗；带参数当命令行用时会自动接管调用方的控制台
%PY% -m PyInstaller --noconfirm --clean --onedir --noconsole --name InviteTool --collect-all playwright gui.py
if errorlevel 1 goto :err

if not exist "dist\InviteTool\data" mkdir "dist\InviteTool\data"
rem 故意**不拷**账号库和站点登记表：打出来的包里不带任何账号 / 站点 / 登录态 / key。
rem 第一次运行时程序自己建空的 data\accounts.json 和 data\sites.json，
rem 账号在「账号池」页导入，站点在「站点」页添加。

echo.
echo 打包完成：dist\InviteTool\InviteTool.exe
echo 包里**没有**你的账号库和站点登记表（第一次运行会自己建空的）。
echo 双击 = 开界面（无黑窗）；命令行用法 InviteTool.exe --checkin / --sites / --export table
echo 把整个 dist\InviteTool 文件夹拷走就能用，data / logs 都在 exe 同目录。
pause
exit /b 0

:err
echo.
echo 打包失败，看上面的报错。没有 exe 不影响使用，双击「启动.cmd」即可。
pause
exit /b 1
