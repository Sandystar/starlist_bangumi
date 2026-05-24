param(
    [string]$AppName = "StarlistBangumi",
    [string]$Version = "",
    [string]$PythonPath = "",
    [switch]$Clean,
    [switch]$SkipTests,
    [switch]$Windowed,
    [switch]$Zip
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$BuildVenv = Join-Path $RepoRoot ".venv-build"
$Python = Join-Path $BuildVenv "Scripts\python.exe"
$DistRoot = Join-Path $RepoRoot "dist\windows"
$WorkRoot = Join-Path $RepoRoot "build\pyinstaller"
$EntryPoint = Join-Path $RepoRoot "src\starlist_bangumi\__main__.py"
$StaticDir = Join-Path $RepoRoot "src\starlist_bangumi\static"
$PromptsDir = Join-Path $RepoRoot "src\starlist_bangumi\prompts"

function Assert-InRepo {
    param([string]$Path)

    $full = [System.IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith($RepoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to operate outside repository: $full"
    }
}

function Remove-DirectoryIfExists {
    param([string]$Path)

    Assert-InRepo $Path
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}

function Write-Utf8File {
    param(
        [string]$Path,
        [string]$Content
    )

    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $utf8NoBom)
}

function Invoke-Native {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($ArgumentList -join ' ')"
    }
}

function Invoke-NativeOutput {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList
    )

    $output = & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($ArgumentList -join ' ')"
    }
    return $output
}

function Test-Python {
    param([string]$Candidate)

    if (-not $Candidate) {
        return $false
    }
    if (-not (Test-Path -LiteralPath $Candidate)) {
        return $false
    }
    $null = & $Candidate --version 2>$null
    return $LASTEXITCODE -eq 0
}

function Resolve-BootstrapPython {
    if ($PythonPath) {
        $resolved = (Resolve-Path -LiteralPath $PythonPath).Path
        if (Test-Python $resolved) {
            return $resolved
        }
        throw "PythonPath is not usable: $PythonPath"
    }

    $candidates = @()
    if ($env:VIRTUAL_ENV) {
        $candidates += Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"
    }
    $candidates += Join-Path $RepoRoot ".venv\Scripts\python.exe"
    $candidates += @(
        Get-Command python -All -CommandType Application -ErrorAction SilentlyContinue |
            Where-Object { $_.Source -notlike "*\WindowsApps\python.exe" } |
            ForEach-Object { $_.Source }
    )

    foreach ($candidate in $candidates) {
        if (Test-Python $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    throw "No usable Python interpreter found. Pass -PythonPath C:\Path\To\python.exe."
}

Set-Location $RepoRoot

if (-not (Test-Path -LiteralPath $EntryPoint)) {
    throw "Entry point not found: $EntryPoint"
}
if (-not (Test-Path -LiteralPath $StaticDir)) {
    throw "Static assets not found: $StaticDir"
}
if (-not (Test-Path -LiteralPath $PromptsDir)) {
    throw "Prompt assets not found: $PromptsDir"
}

if ($Clean) {
    Write-Host "Cleaning build outputs..."
    Remove-DirectoryIfExists $DistRoot
    Remove-DirectoryIfExists $WorkRoot
}

$BootstrapPython = Resolve-BootstrapPython
Write-Host "Using bootstrap Python: $BootstrapPython"

if ((Test-Path -LiteralPath $BuildVenv) -and -not (Test-Path -LiteralPath $Python)) {
    Write-Host "Removing incomplete build virtual environment: $BuildVenv"
    Remove-DirectoryIfExists $BuildVenv
}

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Host "Creating build virtual environment: $BuildVenv"
    Invoke-Native $BootstrapPython @("-m", "venv", $BuildVenv)
}
if (-not (Test-Python $Python)) {
    throw "Build virtual environment Python is not usable: $Python"
}

Write-Host "Installing build dependencies..."
Invoke-Native $Python @("-m", "pip", "install", "--upgrade", "pip")
Invoke-Native $Python @("-m", "pip", "install", "-e", ".[dev]", "pyinstaller")

if (-not $SkipTests) {
    Write-Host "Running tests..."
    Invoke-Native $Python @("-m", "pytest")
}

if (-not $Version) {
    $Version = (Invoke-NativeOutput $Python @("-c", "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")).Trim()
}

New-Item -ItemType Directory -Force -Path $DistRoot | Out-Null
New-Item -ItemType Directory -Force -Path $WorkRoot | Out-Null

$pyinstallerArgs = @(
    "--noconfirm",
    "--clean",
    "--onedir",
    "--name", $AppName,
    "--distpath", $DistRoot,
    "--workpath", $WorkRoot,
    "--specpath", $WorkRoot,
    "--add-data", "$StaticDir;starlist_bangumi\static",
    "--add-data", "$PromptsDir;starlist_bangumi\prompts",
    "--collect-submodules", "webview",
    "--collect-data", "webview",
    "--collect-submodules", "uvicorn",
    "--hidden-import", "uvicorn.lifespan.on",
    "--hidden-import", "uvicorn.loops.asyncio",
    "--hidden-import", "uvicorn.protocols.http.h11_impl"
)

if ($Windowed) {
    $pyinstallerArgs += "--noconsole"
}

$pyinstallerArgs += $EntryPoint

Write-Host "Building $AppName $Version..."
Invoke-Native $Python (@("-m", "PyInstaller") + $pyinstallerArgs)

$PackageDir = Join-Path $DistRoot $AppName
$DataDir = Join-Path $PackageDir "data"
New-Item -ItemType Directory -Force -Path (Join-Path $DataDir "runs") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DataDir "reports") | Out-Null

$ConfigExampleSource = Join-Path $RepoRoot "data\config.example.json"
$ConfigExampleTarget = Join-Path $DataDir "config.example.json"
if (Test-Path -LiteralPath $ConfigExampleSource) {
    Copy-Item -LiteralPath $ConfigExampleSource -Destination $ConfigExampleTarget -Force
} else {
    $configJson = Invoke-NativeOutput $Python @("-c", "import json; from starlist_bangumi.config import AppConfig; print(json.dumps(AppConfig().model_dump(mode='json'), ensure_ascii=False, indent=2))")
    Write-Utf8File $ConfigExampleTarget (($configJson -join [Environment]::NewLine) + [Environment]::NewLine)
}

$Launcher = @"
param(
    [int]`$Port = 8765,
    [switch]`$Web
)

`$ErrorActionPreference = "Stop"
`$Root = `$PSScriptRoot
`$Exe = Join-Path `$Root "$AppName.exe"
`$Data = Join-Path `$Root "data"
`$Config = Join-Path `$Data "config.json"
`$ConfigExample = Join-Path `$Data "config.example.json"

if (-not (Test-Path -LiteralPath `$Config) -and (Test-Path -LiteralPath `$ConfigExample)) {
    Copy-Item -LiteralPath `$ConfigExample -Destination `$Config
}

`$Args = @("--host", "127.0.0.1", "--port", "`$Port")
if (`$Web) {
    `$Args += "--web"
}

& `$Exe @Args
"@
Write-Utf8File (Join-Path $PackageDir "Run-StarlistBangumi.ps1") $Launcher

$Readme = @"
$AppName $Version

Start:
  powershell -ExecutionPolicy Bypass -File .\Run-StarlistBangumi.ps1

Start in browser mode:
  powershell -ExecutionPolicy Bypass -File .\Run-StarlistBangumi.ps1 -Web

Config:
  data\config.json

Notes:
  The package is a PyInstaller onedir build.
  Keep the data folder next to $AppName.exe.
  Runtime records are written to data\runs.
"@
Write-Utf8File (Join-Path $PackageDir "README-WINDOWS.txt") $Readme

if ($Zip) {
    $ZipPath = Join-Path $DistRoot "$AppName-$Version-win-x64.zip"
    if (Test-Path -LiteralPath $ZipPath) {
        Remove-Item -LiteralPath $ZipPath -Force
    }
    Write-Host "Creating archive: $ZipPath"
    Compress-Archive -Path (Join-Path $PackageDir "*") -DestinationPath $ZipPath -Force
}

Write-Host ""
Write-Host "Build complete:"
Write-Host "  $PackageDir"
if ($Zip) {
    Write-Host "  $(Join-Path $DistRoot "$AppName-$Version-win-x64.zip")"
}
