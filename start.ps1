param(
    [int]$Port = 8765,
    [switch]$Desktop
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$listeners = @(Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
foreach ($listener in $listeners) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)" -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        continue
    }
    $commandLine = [string]$process.CommandLine
    if ($commandLine.Contains($PSScriptRoot) -and $commandLine.Contains("-m starlist_bangumi")) {
        Write-Host "Stopping existing Starlist Bangumi process $($listener.OwningProcess) on port $Port"
        Stop-Process -Id $listener.OwningProcess -Force
        continue
    }
    throw "Port $Port is already used by process $($listener.OwningProcess): $commandLine"
}

for ($attempt = 0; $attempt -lt 20; $attempt++) {
    $stillListening = Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($null -eq $stillListening) {
        break
    }
    Start-Sleep -Milliseconds 200
}

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    python -m venv .venv
    & $python -m pip install --upgrade pip
    & $python -m pip install -e ".[dev]"
}

$args = @("-m", "starlist_bangumi", "--host", "127.0.0.1", "--port", "$Port")
if (-not $Desktop) {
    $args += "--web"
}

Write-Host "Starting Starlist Bangumi at http://127.0.0.1:$Port"
& $python @args
