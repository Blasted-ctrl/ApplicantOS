<#
.SYNOPSIS
    One command from a fresh clone of ApplicantOS to a running, seeded backend.

.DESCRIPTION
    Windows is a first-class target for this project, so this is the primary bootstrap and
    `scripts/bootstrap.sh` is its POSIX twin. Run it from anywhere; it locates the repository
    from its own path rather than trusting the working directory.

    Steps, in order, each one skipped when it has already been done:

      1. verify Python 3.12+
      2. create the virtualenv (.venv)
      3. install the serving stack, the project, and the dev toolchain
      4. install the Playwright Chromium browser
      5. copy .env.example to .env if there is no .env
      6. start PostgreSQL and Redis with Docker  (-Mode postgres only)
      7. apply migrations
      8. seed a user, a profile and a knowledge graph

    The default mode is `sqlite`: no Docker, no PostgreSQL, no Redis, no API keys. That is
    the posture docs/WORKING_AGREEMENT.md §6 requires the whole pipeline to run in, and it is
    the fastest path from `git clone` to a generated resume. `-Mode postgres` brings up the
    compose infrastructure instead.

    Nothing here flips a safety switch. AUTO_APPLY_ENABLED and DRY_RUN keep the values
    .env.example ships, which are the safe ones (golden rule #3).

.PARAMETER Mode
    `sqlite` (default) for the zero-infrastructure install, or `postgres` to start the
    PostgreSQL and Redis containers and point the backend at them.

.PARAMETER SkipBrowser
    Skip `playwright install chromium`. Saves a ~150MB download when you only intend to work
    on discovery, scoring or the knowledge engine — nothing outside app/browser/ needs it.

.PARAMETER SkipSeed
    Install and migrate, but leave the database empty.

.PARAMETER Python
    The interpreter used to create the virtualenv. Defaults to `py -3.12` when the Python
    launcher has that version, otherwise `python`.

.EXAMPLE
    .\scripts\bootstrap.ps1

.EXAMPLE
    .\scripts\bootstrap.ps1 -Mode postgres

.EXAMPLE
    .\scripts\bootstrap.ps1 -SkipBrowser
#>

[CmdletBinding()]
param(
    [ValidateSet('sqlite', 'postgres')]
    [string]$Mode = 'sqlite',

    [switch]$SkipBrowser,

    [switch]$SkipSeed,

    [string]$Python = ''
)

# Stop on the first failure. Without this a failed `pip install` is a warning and the script
# happily goes on to migrate a database whose driver was never installed.
$ErrorActionPreference = 'Stop'

# ----------------------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------------------

# The floor from pyproject.toml's `requires-python`. Below it, `Mapped[]` declarative syntax
# and PEP 695-adjacent typing in the models do not parse.
$MinimumPythonMajor = 3
$MinimumPythonMinor = 12

$VenvDirName = '.venv'

# ----------------------------------------------------------------------------------------
# Output helpers
# ----------------------------------------------------------------------------------------

function Write-Step {
    param([string]$Message)
    Write-Host ''
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Note {
    param([string]$Message)
    Write-Host "    $Message" -ForegroundColor DarkGray
}

function Write-Ok {
    param([string]$Message)
    Write-Host "    $Message" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host "    $Message" -ForegroundColor Yellow
}

function Assert-LastExitCode {
    param([string]$What)
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed with exit code $LASTEXITCODE"
    }
}

# ----------------------------------------------------------------------------------------
# Locate the repository
# ----------------------------------------------------------------------------------------

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir

if (-not (Test-Path (Join-Path $RepoRoot 'pyproject.toml'))) {
    throw "cannot find pyproject.toml above $ScriptDir - is this a full clone?"
}

Set-Location $RepoRoot

Write-Host ''
Write-Host 'ApplicantOS bootstrap' -ForegroundColor White
Write-Note "repository : $RepoRoot"
Write-Note "mode       : $Mode"

# ----------------------------------------------------------------------------------------
# 1. Interpreter
# ----------------------------------------------------------------------------------------

Write-Step 'Checking Python'

function Resolve-BootstrapPython {
    param([string]$Requested)

    if ($Requested) { return $Requested }

    # The Python launcher is the reliable way to pick a specific version on Windows; a bare
    # `python` may be the Microsoft Store shim, which cannot create a working venv.
    $launcher = Get-Command 'py' -ErrorAction SilentlyContinue
    if ($launcher) {
        $versions = & py --list 2>$null
        if ($LASTEXITCODE -eq 0 -and ($versions -match '3\.1[2-9]')) {
            return 'py -3'
        }
    }
    return 'python'
}

$BootstrapPython = Resolve-BootstrapPython -Requested $Python
Write-Note "interpreter: $BootstrapPython"

# Split so `py -3` works as a command plus argument.
$pythonParts = $BootstrapPython.Split(' ')
$pythonExe = $pythonParts[0]
$pythonArgs = @()
if ($pythonParts.Length -gt 1) { $pythonArgs = $pythonParts[1..($pythonParts.Length - 1)] }

$versionText = & $pythonExe @pythonArgs -c "import sys; print('%d.%d' % sys.version_info[:2])"
Assert-LastExitCode 'python --version'

$versionParts = $versionText.Trim().Split('.')
$major = [int]$versionParts[0]
$minor = [int]$versionParts[1]

if ($major -lt $MinimumPythonMajor -or ($major -eq $MinimumPythonMajor -and $minor -lt $MinimumPythonMinor)) {
    throw "Python $MinimumPythonMajor.$MinimumPythonMinor+ is required; found $versionText. Install it from python.org and re-run, or pass -Python."
}
Write-Ok "Python $versionText"

# ----------------------------------------------------------------------------------------
# 2. Virtualenv
# ----------------------------------------------------------------------------------------

$VenvPath = Join-Path $RepoRoot $VenvDirName

# The interpreter lives in `Scripts\` on Windows and `bin/` on POSIX. Both are probed even
# though this script is Windows-first: PowerShell 7 runs on Linux and macOS, and a virtualenv
# created there has the POSIX layout. Assuming one layout would report "no virtualenv", run
# `venv` on top of a working one, and leave a hybrid with two half-populated directories and
# a `pyvenv.cfg` naming an interpreter that owns neither.
function Resolve-VenvPython {
    $windows = Join-Path $VenvPath 'Scripts\python.exe'
    if (Test-Path $windows) { return $windows }
    $posix = Join-Path $VenvPath 'bin/python'
    if (Test-Path $posix) { return $posix }
    return $null
}

$VenvPython = Resolve-VenvPython

if ($VenvPython) {
    Write-Step "Virtualenv already exists ($VenvDirName)"
} else {
    Write-Step "Creating virtualenv ($VenvDirName)"
    & $pythonExe @pythonArgs -m venv $VenvPath
    Assert-LastExitCode 'python -m venv'
    $VenvPython = Resolve-VenvPython
    if (-not $VenvPython) {
        throw "python -m venv reported success but produced no interpreter in $VenvPath"
    }
    Write-Ok 'created'
}

# Every command below names the interpreter explicitly instead of activating. Activation
# mutates the caller's shell, which is rude for a script and silently wrong when the script
# is invoked from an editor or a task runner.

# ----------------------------------------------------------------------------------------
# 3. Dependencies
# ----------------------------------------------------------------------------------------

Write-Step 'Installing dependencies'

& $VenvPython -m pip install --upgrade --quiet pip setuptools wheel
Assert-LastExitCode 'pip install --upgrade pip'

Write-Note 'serving stack (docker/requirements-runtime.txt)'
& $VenvPython -m pip install --quiet --requirement (Join-Path $RepoRoot 'docker\requirements-runtime.txt')
Assert-LastExitCode 'pip install -r requirements-runtime.txt'

# The database driver is chosen at install time, not at run time: app/database/session.py
# builds the engine while it is being imported, so the driver named by DATABASE_URL has to be
# present before anything under app.database can even be imported.
if ($Mode -eq 'postgres') {
    $extras = '.[postgres,redis,orjson,pgvector]'
} else {
    $extras = '.[sqlite,orjson]'
}
Write-Note "project, editable: $extras"
& $VenvPython -m pip install --quiet --editable $extras
Assert-LastExitCode 'pip install -e .'

Write-Note 'dev toolchain (ruff, mypy, pytest)'
& $VenvPython -m pip install --quiet ruff mypy pytest pytest-asyncio pytest-cov
Assert-LastExitCode 'pip install dev toolchain'

Write-Note 'browser automation and document rendering'
& $VenvPython -m pip install --quiet playwright pypdf python-docx reportlab selectolax
Assert-LastExitCode 'pip install worker packages'

Write-Ok 'dependencies installed'

# ----------------------------------------------------------------------------------------
# 4. Browser
# ----------------------------------------------------------------------------------------

if ($SkipBrowser) {
    Write-Step 'Skipping the Playwright browser (-SkipBrowser)'
    Write-Warn 'app/browser/ will not run until you install it: .venv\Scripts\playwright install chromium'
} else {
    Write-Step 'Installing the Playwright Chromium browser (~150MB, cached after the first run)'
    & $VenvPython -m playwright install chromium
    Assert-LastExitCode 'playwright install chromium'
    Write-Ok 'chromium ready'
}

# ----------------------------------------------------------------------------------------
# 5. Environment file
# ----------------------------------------------------------------------------------------

$EnvFile = Join-Path $RepoRoot '.env'
$EnvExample = Join-Path $RepoRoot '.env.example'

if (Test-Path $EnvFile) {
    Write-Step '.env already exists - leaving it alone'
} else {
    Write-Step 'Creating .env from .env.example'
    Copy-Item $EnvExample $EnvFile
    Write-Ok 'created (AUTO_APPLY_ENABLED=false, DRY_RUN=true - the safe posture)'
}

# ----------------------------------------------------------------------------------------
# 6. Infrastructure
# ----------------------------------------------------------------------------------------

# Passed to every backend command below. In sqlite mode this is the whole zero-dependency
# posture; in postgres mode it is empty and .env decides.
$RuntimeEnv = @{}
if ($Mode -eq 'sqlite') {
    $RuntimeEnv = @{
        'SQLITE_MODE'        = 'true'
        'LLM_PROVIDER'       = 'null'
        'EMBEDDING_PROVIDER' = 'hashing'
        'VECTOR_STORE'       = 'memory'
    }
}

function Invoke-WithRuntimeEnv {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$What
    )
    $saved = @{}
    foreach ($key in $RuntimeEnv.Keys) {
        $saved[$key] = [Environment]::GetEnvironmentVariable($key)
        Set-Item -Path "env:$key" -Value $RuntimeEnv[$key]
    }
    try {
        & $VenvPython @Arguments
        Assert-LastExitCode $What
    } finally {
        # Restore rather than delete: the caller may legitimately have had these set, and a
        # bootstrap script that edits the ambient environment on its way out is a trap.
        foreach ($key in $saved.Keys) {
            if ($null -eq $saved[$key]) {
                Remove-Item -Path "env:$key" -ErrorAction SilentlyContinue
            } else {
                Set-Item -Path "env:$key" -Value $saved[$key]
            }
        }
    }
}

if ($Mode -eq 'postgres') {
    Write-Step 'Starting PostgreSQL and Redis'

    $docker = Get-Command 'docker' -ErrorAction SilentlyContinue
    if (-not $docker) {
        throw 'docker was not found on PATH. Install Docker Desktop, or re-run without -Mode postgres to use the zero-infrastructure SQLite install.'
    }

    & docker compose up -d postgres redis
    Assert-LastExitCode 'docker compose up postgres redis'

    Write-Note 'waiting for both to report healthy'
    $deadline = (Get-Date).AddSeconds(120)
    while ($true) {
        $states = & docker compose ps --format json 2>$null
        $healthy = ($states -match '"Health":\s*"healthy"').Count
        if ($healthy -ge 2) { break }
        if ((Get-Date) -gt $deadline) {
            throw 'postgres and redis did not become healthy within 120s. Check `docker compose logs postgres redis`.'
        }
        Start-Sleep -Seconds 2
    }
    Write-Ok 'postgres and redis are healthy'
} else {
    Write-Step 'Zero-infrastructure mode - no PostgreSQL, no Redis, no API keys'
    Write-Note 'SQLITE_MODE=true  LLM_PROVIDER=null  EMBEDDING_PROVIDER=hashing  VECTOR_STORE=memory'
}

# ----------------------------------------------------------------------------------------
# 7. Migrations
# ----------------------------------------------------------------------------------------

Write-Step 'Applying migrations'
Invoke-WithRuntimeEnv -Arguments @('-m', 'alembic', 'upgrade', 'head') -What 'alembic upgrade head'
Write-Ok 'schema is at head'

# ----------------------------------------------------------------------------------------
# 8. Seed
# ----------------------------------------------------------------------------------------

if ($SkipSeed) {
    Write-Step 'Skipping the seed (-SkipSeed)'
} else {
    Write-Step 'Seeding a user, a profile and a knowledge graph'
    Invoke-WithRuntimeEnv -Arguments @('-m', 'scripts.seed') -What 'python -m scripts.seed'
}

# ----------------------------------------------------------------------------------------
# Done
# ----------------------------------------------------------------------------------------

$prefix = ''
if ($Mode -eq 'sqlite') {
    $prefix = '$env:SQLITE_MODE="true"; $env:LLM_PROVIDER="null"; $env:EMBEDDING_PROVIDER="hashing"; $env:VECTOR_STORE="memory"; '
}

Write-Host ''
Write-Host 'Ready.' -ForegroundColor Green
Write-Host ''
Write-Host '  Serve the API:'
Write-Host "    $prefix.\$VenvDirName\Scripts\python.exe -m uvicorn app.main:app --reload"
Write-Host ''
Write-Host '  Prove the whole surface answers:'
Write-Host "    $prefix.\$VenvDirName\Scripts\python.exe -m scripts.smoke_test --start"
Write-Host ''
Write-Host '  Run the desktop app:'
Write-Host '    cd desktop; npm install; npm run dev'
Write-Host ''
Write-Host '  Safety posture: AUTO_APPLY_ENABLED=false, DRY_RUN=true.' -ForegroundColor DarkGray
Write-Host '  Nothing is submitted anywhere until you flip both, deliberately, in .env.' -ForegroundColor DarkGray
Write-Host ''
