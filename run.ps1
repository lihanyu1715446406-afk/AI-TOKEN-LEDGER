# AI Token Ledger - Windows Launcher
# 用法: 右键“使用 PowerShell 运行”，或双击桌面快捷方式

Write-Host "===========================================" -ForegroundColor Cyan
Write-Host "  AI Token Ledger" -ForegroundColor White
Write-Host "===========================================" -ForegroundColor Cyan
Write-Host ""

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir


function Find-Python {
    # 返回数组: [可执行文件, 附加参数...]；找不到返回 $null
    # 按可靠性从高到低尝试，不写死任何具体版本号

    # 1) py 启动器——Windows 官方推荐，能自动挑一个可用的 Python 3
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { return @($py.Source, "-3") }

    # 2) 注册表中登记的安装（按版本号降序，优先新版）
    foreach ($root in @("HKCU:\SOFTWARE\Python\PythonCore", "HKLM:\SOFTWARE\Python\PythonCore")) {
        if (-not (Test-Path $root)) { continue }
        $versions = Get-ChildItem $root -ErrorAction SilentlyContinue | Sort-Object Name -Descending
        foreach ($ver in $versions) {
            $key = Get-Item (Join-Path $ver.PSPath "InstallPath") -ErrorAction SilentlyContinue
            if (-not $key) { continue }
            $dir = $key.GetValue("")
            if ($dir) {
                $exe = Join-Path $dir "python.exe"
                if (Test-Path $exe) { return @($exe) }
            }
        }
    }

    # 3) PATH 中的 python
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return @($cmd.Source) }

    # 4) 兜底：常见安装位置的版本化目录
    foreach ($pattern in @(
        "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
        "$env:ProgramFiles\Python3*\python.exe",
        "C:\Python3*\python.exe"
    )) {
        $hit = Get-ChildItem $pattern -ErrorAction SilentlyContinue |
               Sort-Object FullName -Descending | Select-Object -First 1
        if ($hit) { return @($hit.FullName) }
    }

    return $null
}


$Python = Find-Python
if (-not $Python) {
    Write-Host "ERROR: 未找到 Python 3。" -ForegroundColor Red
    Write-Host "请先安装: https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host "（安装时请勾选 Add Python to PATH）" -ForegroundColor Yellow
    Read-Host "按回车退出"
    exit 1
}

$PythonExe = $Python[0]
$PythonArgs = @()
if ($Python.Count -gt 1) { $PythonArgs = $Python[1..($Python.Count - 1)] }

Write-Host "Python: $PythonExe $($PythonArgs -join ' ')" -ForegroundColor Gray
Write-Host "[1/2] 正在同步数据..." -ForegroundColor Yellow

$result = & $PythonExe @PythonArgs "$ScriptDir\sync_tokens.py"
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "同步失败！" -ForegroundColor Red
    Write-Host $result
    Read-Host "按回车退出"
    exit 1
}
Write-Host $result

Write-Host ""
Write-Host "[2/2] 正在打开仪表盘..." -ForegroundColor Yellow
$dashboardPath = Join-Path $ScriptDir "dashboard.html"
Start-Process $dashboardPath

Write-Host ""
Write-Host "仪表盘已打开。再次双击快捷方式即可重新同步。" -ForegroundColor Green
Write-Host ""
Read-Host "按回车退出"
