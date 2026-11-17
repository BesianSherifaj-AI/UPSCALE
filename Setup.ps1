param([string]$Python = '')

$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
$taskEngine = Join-Path $taskRoot 'engine'
$taskRepository = 'https://github.com/Konohamaru04/ComfyUI-NVIDIA-DLSS-Frame-Interpolation.git'
$taskEngineCommit = 'c755e274a405a7a47667bd567d489b6845066bcf'
$taskEnginePatch = Join-Path $taskRoot 'patches\engine-performance.patch'
$taskLegacyEnginePatch = Join-Path $taskRoot 'patches\engine-performance-v1.patch'
$taskEngineUpgradePatch = Join-Path $taskRoot 'patches\engine-performance-upgrade-v1.patch'
$taskCancellationPatch = Join-Path $taskRoot 'patches\engine-cancellation.patch'

function Assert-Exit([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed with exit code $LASTEXITCODE." }
}

function Install-EnginePatch(
    [string]$GitPath,
    [string]$EnginePath,
    [string]$PatchPath,
    [string]$LegacyPatchPath,
    [string]$UpgradePatchPath
) {
    foreach ($taskRequiredPatch in @($PatchPath, $LegacyPatchPath, $UpgradePatchPath)) {
        if (-not (Test-Path -LiteralPath $taskRequiredPatch -PathType Leaf)) {
            throw "A bundled engine patch is missing: $taskRequiredPatch. Restore the complete UPSCALE folder, then rerun Setup.ps1."
        }
    }
    # Recognize current, clean and the previously shipped v1 engine before
    # writing. An upgrade is one checked patch, never a reset or uninstall.
    $taskPreviousErrorAction = $ErrorActionPreference
    $taskPatchApplied = $false
    $taskPatchCanApply = $false
    $taskLegacyApplied = $false
    $taskUpgradeCanApply = $false
    $taskPatchErrors = @()
    try {
        # Windows PowerShell 5.1 treats redirected native stderr as ErrorRecords;
        # failed check commands are expected while detecting an install state.
        $ErrorActionPreference = 'Continue'
        & $GitPath -C $EnginePath apply --reverse --check $PatchPath *> $null
        $taskPatchApplied = $LASTEXITCODE -eq 0
        if (-not $taskPatchApplied) {
            $taskPatchErrors = @(& $GitPath -C $EnginePath apply --check $PatchPath 2>&1)
            $taskPatchCanApply = $LASTEXITCODE -eq 0
        }
        if (-not $taskPatchApplied -and -not $taskPatchCanApply) {
            & $GitPath -C $EnginePath apply --reverse --check $LegacyPatchPath *> $null
            $taskLegacyApplied = $LASTEXITCODE -eq 0
            if ($taskLegacyApplied) {
                $taskPatchErrors = @(& $GitPath -C $EnginePath apply --check $UpgradePatchPath 2>&1)
                $taskUpgradeCanApply = $LASTEXITCODE -eq 0
            }
        }
    } finally {
        $ErrorActionPreference = $taskPreviousErrorAction
    }
    if ($taskPatchApplied) {
        Write-Host 'The bundled engine performance and audio fixes are already installed.'
    } elseif ($taskPatchCanApply) {
        Write-Host 'Installing the bundled engine performance and audio fixes...'
        & $GitPath -C $EnginePath apply $PatchPath
        Assert-Exit 'Applying the bundled engine fixes'
    } elseif ($taskLegacyApplied -and $taskUpgradeCanApply) {
        Write-Host 'Upgrading the existing engine with parallel processing and performance fixes...'
        & $GitPath -C $EnginePath apply $UpgradePatchPath
        Assert-Exit 'Upgrading the bundled engine fixes'
    } else {
        $taskPatchDetail = ($taskPatchErrors | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
        throw "The bundled engine patch conflicts with this engine version or local edits. Existing files were not reset or overwritten. Review patches\engine-performance.patch against engine, or keep a backup of engine and rerun Setup.ps1 with that folder moved aside to install the tested version. Git reported: $taskPatchDetail"
    }
}

function Install-CancellationPatch([string]$GitPath, [string]$EnginePath, [string]$PatchPath) {
    if (-not (Test-Path -LiteralPath $PatchPath -PathType Leaf)) {
        throw "The bundled cancellation fix is missing: $PatchPath. Restore the complete patches folder."
    }
    $taskPreviousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $GitPath -C $EnginePath apply --reverse --check $PatchPath *> $null
        $taskApplied = $LASTEXITCODE -eq 0
        if (-not $taskApplied) {
            & $GitPath -C $EnginePath apply --check $PatchPath *> $null
            $taskCanApply = $LASTEXITCODE -eq 0
        }
    } finally {
        $ErrorActionPreference = $taskPreviousErrorAction
    }
    if ($taskApplied) {
        Write-Host 'The cancellation cleanup fix is already installed.'
    } elseif ($taskCanApply) {
        & $GitPath -C $EnginePath apply $PatchPath
        Assert-Exit 'Installing the cancellation cleanup fix'
    } else {
        throw 'The cancellation fix conflicts with this engine version or local edits. Existing files were kept. Review patches\engine-cancellation.patch against the engine.'
    }
}

try {
    if (-not (Test-Path -LiteralPath $taskPython -PathType Leaf)) {
        $taskBasePython = $Python
        if (-not $taskBasePython) {
            $taskLauncher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue
            if ($taskLauncher) {
                foreach ($taskVersion in @('-3.11', '-3.12', '-3.13')) {
                    $taskFound = & $taskLauncher.Source $taskVersion -c 'import sys; print(sys.executable)' 2>$null
                    if ($LASTEXITCODE -eq 0 -and $taskFound) {
                        $taskBasePython = [string]($taskFound | Select-Object -Last 1)
                        break
                    }
                }
            }
            if (-not $taskBasePython) {
                $taskCommand = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue
                if ($taskCommand) { $taskBasePython = $taskCommand.Source }
            }
        }
        if (-not $taskBasePython) {
            throw 'Python 3.11 or later was not found. Install Python or rerun with -Python "C:\path\to\python.exe".'
        }
        & $taskBasePython -c 'import sys; assert (3,11) <= sys.version_info[:2] < (3,14), "Use Python 3.11, 3.12, or 3.13"'
        Assert-Exit 'Python version check'
        Write-Host 'Creating the local Python environment...'
        & $taskBasePython -m venv (Join-Path $taskRoot '.venv')
        Assert-Exit 'Creating the environment'
    }

    Write-Host 'Installing the GUI and video dependencies into .venv...'
    & $taskPython -m pip install --disable-pip-version-check -r (Join-Path $taskRoot 'requirements.txt')
    Assert-Exit 'Installing dependencies'

    $taskGit = Get-Command git.exe -CommandType Application -ErrorAction SilentlyContinue
    if (-not $taskGit) { throw 'Git is needed to verify and install the bundled engine fixes. Install Git, then rerun Setup.ps1.' }
    if (-not (Test-Path -LiteralPath $taskEnginePatch -PathType Leaf)) {
        throw "The bundled engine patch is missing: $taskEnginePatch. Restore the complete UPSCALE folder, then rerun Setup.ps1."
    }
    if (-not (Test-Path -LiteralPath $taskEngine)) {
        & $taskGit.Source lfs version
        Assert-Exit 'Git LFS check'
        Write-Host 'Downloading the NVIDIA engine and its bundled runtime files...'
        $taskOldSkipSmudge = [Environment]::GetEnvironmentVariable('GIT_LFS_SKIP_SMUDGE', 'Process')
        try {
            $env:GIT_LFS_SKIP_SMUDGE = '1'
            & $taskGit.Source clone $taskRepository $taskEngine
            Assert-Exit 'Cloning the engine'
            & $taskGit.Source -C $taskEngine checkout --detach $taskEngineCommit
            Assert-Exit 'Selecting the tested engine version'
        } finally {
            [Environment]::SetEnvironmentVariable('GIT_LFS_SKIP_SMUDGE', $taskOldSkipSmudge, 'Process')
        }
        & $taskGit.Source -C $taskEngine lfs pull
        Assert-Exit 'Downloading engine runtime binaries'
    } else {
        Write-Host 'Keeping the existing engine folder and version.'
    }

    Install-EnginePatch -GitPath $taskGit.Source -EnginePath $taskEngine -PatchPath $taskEnginePatch -LegacyPatchPath $taskLegacyEnginePatch -UpgradePatchPath $taskEngineUpgradePatch
    Install-CancellationPatch -GitPath $taskGit.Source -EnginePath $taskEngine -PatchPath $taskCancellationPatch

    & $taskPython -c 'import PySide6.QtWidgets, numpy, cv2, av; print("Python dependencies ready.")'
    Assert-Exit 'Checking Python dependencies'
    foreach ($taskTool in @('ffmpeg', 'ffprobe')) {
        $taskOverride = [Environment]::GetEnvironmentVariable("DLSS_$($taskTool.ToUpperInvariant())_PATH")
        $taskExecutable = $taskOverride
        if (-not $taskExecutable) {
            $taskCommand = Get-Command "$taskTool.exe" -CommandType Application -ErrorAction SilentlyContinue
            if ($taskCommand) { $taskExecutable = $taskCommand.Source }
        }
        if (-not $taskExecutable -or -not (Test-Path -LiteralPath $taskExecutable -PathType Leaf)) {
            throw "$taskTool.exe was not found. Add its folder to PATH or set DLSS_$($taskTool.ToUpperInvariant())_PATH to its full executable path. Setup makes no system-wide changes."
        }
        & $taskExecutable -version | Select-Object -First 1
        Assert-Exit "Checking $taskTool"
    }
    Write-Host "`nSetup complete. Double-click Launch Upscale.vbs." -ForegroundColor Green
} catch {
    Write-Host "`nSetup could not finish: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
