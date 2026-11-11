# BatchCut Studio - open UI / start service
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 mis-decodes
# non-ASCII in .ps1 files without a BOM, which breaks paths.
Set-Location -LiteralPath $PSScriptRoot

$info = $null
if (Test-Path -LiteralPath 'running.json') {
    try {
        $info = Get-Content -LiteralPath 'running.json' -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        $info = $null
    }
}

# running.json may be stale (hard-killed process): verify the PID is alive
if ($info -ne $null -and $info.pid -ne $null) {
    $alive = Get-Process -Id $info.pid -ErrorAction SilentlyContinue
    if ($alive -ne $null) {
        Write-Host ("Open " + $info.url) -ForegroundColor Cyan
        Start-Process $info.url
        exit 0
    }
    Remove-Item -LiteralPath 'running.json' -Force -ErrorAction SilentlyContinue
}

# Probe the usual port range in case an instance is running without running.json
$found = $null
foreach ($p in 8963..9002) {
    try {
        $r = Invoke-WebRequest -Uri ("http://127.0.0.1:" + $p + "/api/state") -TimeoutSec 1 -UseBasicParsing
        if ($r.StatusCode -eq 200) {
            $found = "http://127.0.0.1:" + $p
            break
        }
    } catch {
    }
}

if ($found -ne $null) {
    Write-Host ("Found running service: " + $found) -ForegroundColor Cyan
    Start-Process $found
    exit 0
}

# Start the service directly (avoid non-ASCII .bat names in PS 5.1)
Write-Host "Service not running, starting..." -ForegroundColor Yellow
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    Write-Host "python not found in PATH" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}
# 路径含空格必须显式加引号传参,否则 Start-Process 会把路径拆成多个参数
$serverPath = Join-Path -Path $PSScriptRoot -ChildPath 'server.py'
$argList = @('-u', ('"' + $serverPath + '"'))
Start-Process -FilePath $python `
    -ArgumentList $argList `
    -WorkingDirectory $PSScriptRoot `
    -WindowStyle Minimized
Write-Host "Starting... browser opens automatically in a few seconds." -ForegroundColor Green
