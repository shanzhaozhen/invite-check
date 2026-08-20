@echo off
cd /d "%~dp0.."
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
title 导出 API Key（全部站点）

if not exist "exports" mkdir "exports"
echo === 全部站点：账号 + 站点 + API Key ===
%PY% cli.py --export table --all-sites
%PY% cli.py --export table --all-sites --export-out exports\keys-all-table.txt
%PY% cli.py --export keys  --all-sites --export-out exports\keys-all.txt
%PY% cli.py --export csv   --all-sites --export-out exports\keys-all.csv
echo.
echo 已写出 exports\keys-all-table.txt / keys-all.txt / keys-all.csv（表格和 CSV 带站点列）
pause
