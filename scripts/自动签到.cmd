@echo off
cd /d "%~dp0.."
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
title 自动签到（全部启用站点）

echo 开始自动签到：所有启用的站点依次跑，全程无头、不占屏幕和鼠标。
echo 每个账号按这个顺序试，成了就记进 sites.json，下次这个站点直接用那种：
echo   1. 接口直签（站点没开人机验证时，约 1 秒）
echo   2. CloakBrowser 在承载页上取 Turnstile 令牌，再调签到接口（约 14 秒）
echo   3. CloakBrowser 走站点界面：头像 - 个人资料 - 立即签到 - 有验证就自动勾（约 25 秒）
echo （当天已签过的、以及没在该站点注册成功的会自动跳过）
echo.
%PY% cli.py --site all --checkin
echo.
echo 签到结束，详细记录见 logs\<站点>\checkin.log
pause
