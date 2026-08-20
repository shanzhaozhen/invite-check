@echo off
cd /d "%~dp0.."
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
title 协助签到（工具先试，不成你点一下）

echo 这个模式用 CloakBrowser 逐个打开已登录好的窗口（能看见）：
echo   工具先自己走一遍：个人资料 - 立即签到 - 有人机验证就勾一下
echo   自动没成的才轮到你：在窗口里点「每日签到」，签上工具会自动关窗口继续下一个
echo 窗口里的人机验证是能点过的，所以多数账号你不用动手。
echo 今天已签到的、没在该站点注册成功的会自动跳过。
echo.
pause
%PY% cli.py --site all --checkin-assist
echo.
echo 结束，详细记录见 logs\<站点>\checkin.log
pause
