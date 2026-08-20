"""生成双击入口脚本（.cmd 用 GBK 编码，.ps1 用带 BOM 的 UTF-8）。

脚本都放在 `scripts/`，里面统一先 `cd` 回项目根目录再干活——数据在根目录的
data / logs / exports 里，工作目录必须是根。

cmd.exe 按系统 ANSI 代码页解析批处理文件，UTF-8 的中文会被当成乱码并把命令行解析坏，
所以这些文件必须用 GBK 写；Windows PowerShell 5.1 则需要 BOM 才认 UTF-8。
改入口脚本就改这个文件，然后跑 `python make_launchers.py` 重新生成。
"""

from __future__ import annotations

from pathlib import Path

BASE = Path(__file__).resolve().parent
SCRIPTS = BASE / "scripts"

HEAD = """@echo off
cd /d "%~dp0.."
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
"""

GUI = HEAD + """title 邀请站点工具 InviteTool

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
"""

CHECKIN = HEAD + """title 自动签到（全部启用站点）

echo 开始自动签到：所有启用的站点依次跑，全程无头、不占屏幕和鼠标。
echo 每个账号按这个顺序试，成了就记进 sites.json，下次这个站点直接用那种：
echo   1. 接口直签（站点没开人机验证时，约 1 秒）
echo   2. CloakBrowser 在承载页上取 Turnstile 令牌，再调签到接口（约 14 秒）
echo   3. CloakBrowser 走站点界面：头像 - 个人资料 - 立即签到 - 有验证就自动勾（约 25 秒）
echo （当天已签过的、以及没在该站点注册成功的会自动跳过）
echo.
%PY% cli.py --site all --checkin
echo.
echo 签到结束，详细记录见 logs\\<站点>\\checkin.log
pause
"""

ASSIST = HEAD + """title 协助签到（工具先试，不成你点一下）

echo 这个模式用 CloakBrowser 逐个打开已登录好的窗口（能看见）：
echo   工具先自己走一遍：个人资料 - 立即签到 - 有人机验证就勾一下
echo   自动没成的才轮到你：在窗口里点「每日签到」，签上工具会自动关窗口继续下一个
echo 窗口里的人机验证是能点过的，所以多数账号你不用动手。
echo 今天已签到的、没在该站点注册成功的会自动跳过。
echo.
pause
%PY% cli.py --site all --checkin-assist
echo.
echo 结束，详细记录见 logs\\<站点>\\checkin.log
pause
"""

EXPORT = HEAD + """title 导出 API Key（全部站点）

if not exist "exports" mkdir "exports"
echo === 全部站点：账号 + 站点 + API Key ===
%PY% cli.py --export table --all-sites
%PY% cli.py --export table --all-sites --export-out exports\\keys-all-table.txt
%PY% cli.py --export keys  --all-sites --export-out exports\\keys-all.txt
%PY% cli.py --export csv   --all-sites --export-out exports\\keys-all.csv
echo.
echo 已写出 exports\\keys-all-table.txt / keys-all.txt / keys-all.csv（表格和 CSV 带站点列）
pause
"""

BUILD_EXE = HEAD + """title 打包 exe

echo 正在安装/更新 PyInstaller...
%PY% -m pip install --quiet --upgrade pyinstaller
if errorlevel 1 goto :err

echo 正在打包，需要几分钟，别关窗口...
rem --noconsole：窗口程序，双击不会多一个黑窗；带参数当命令行用时会自动接管调用方的控制台
%PY% -m PyInstaller --noconfirm --clean --onedir --noconsole --name InviteTool --collect-all playwright gui.py
if errorlevel 1 goto :err

if not exist "dist\\InviteTool\\data" mkdir "dist\\InviteTool\\data"
rem 故意**不拷**账号库和站点登记表：打出来的包里不带任何账号 / 站点 / 登录态 / key。
rem 第一次运行时程序自己建空的 data\\accounts.json 和 data\\sites.json，
rem 账号在「账号池」页导入，站点在「站点」页添加。

echo.
echo 打包完成：dist\\InviteTool\\InviteTool.exe
echo 包里**没有**你的账号库和站点登记表（第一次运行会自己建空的）。
echo 双击 = 开界面（无黑窗）；命令行用法 InviteTool.exe --checkin / --sites / --export table
echo 把整个 dist\\InviteTool 文件夹拷走就能用，data / logs 都在 exe 同目录。
pause
exit /b 0

:err
echo.
echo 打包失败，看上面的报错。没有 exe 不影响使用，双击「启动.cmd」即可。
pause
exit /b 1
"""

PS1 = """# 启动图形界面（PowerShell 版）。右键「使用 PowerShell 运行」即可。
Set-Location (Join-Path $PSScriptRoot '..')
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$py = if (Get-Command py -ErrorAction SilentlyContinue) { 'py' } else { 'python' }

& $py -c "import playwright, cloakbrowser" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host '第一次运行或依赖有更新，正在安装依赖，请稍等...'
    & $py -m pip install -r requirements.txt
    & $py -c "import playwright" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host '依赖安装失败：请确认装了 Python 3.10 以上，并且网络可用。'
        Read-Host '按回车退出'
        exit 1
    }
}

& $py gui.py
if ($LASTEXITCODE -ne 0) { Read-Host '程序异常退出，按回车关闭' }
"""

FILES = {
    "启动.cmd": (GUI, "gbk"),
    "自动签到.cmd": (CHECKIN, "gbk"),
    "协助签到.cmd": (ASSIST, "gbk"),
    "导出Key.cmd": (EXPORT, "gbk"),
    "构建exe.cmd": (BUILD_EXE, "gbk"),
    "启动.ps1": (PS1, "utf-8-sig"),
}


def main() -> int:
    SCRIPTS.mkdir(parents=True, exist_ok=True)
    for name, (text, encoding) in FILES.items():
        path = SCRIPTS / name
        path.write_text(text.replace("\n", "\r\n"), encoding=encoding, newline="")
        print(f"已生成 scripts/{name}（{encoding}，{path.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
