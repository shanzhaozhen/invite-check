# 启动图形界面（PowerShell 版）。右键「使用 PowerShell 运行」即可。
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
