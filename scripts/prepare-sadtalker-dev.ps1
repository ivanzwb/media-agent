param(
    [string]$DataDir = "",
    [string]$Proxy = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$env:PYTHONUTF8 = "1"
$env:CONDA_SSL_VERIFY = "false"
$env:CONDA_NOTICES_DISABLED = "1"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $DataDir) { $DataDir = Join-Path $Root "data" }
$BuildRoot = Join-Path $Root ".runtime-build"
$MinicondaDir = Join-Path $BuildRoot "miniconda"
$MinicondaVersion = "py310_24.7.1-0"
$MinicondaMarker = Join-Path $MinicondaDir ".media-agent-version"
$EnvDir = Join-Path $BuildRoot "sadtalker-env"
$ArtifactDir = Join-Path $BuildRoot "artifacts"
$WheelDir = Join-Path $BuildRoot "wheels"
$SourceDir = Join-Path $EnvDir "sadtalker-src"
$SourceCommit = "cd4c0465ae0b54a6f85af57f5c65fec9fe23e7f8"

New-Item -ItemType Directory -Force -Path $BuildRoot, $ArtifactDir, $WheelDir | Out-Null

$CondaCommand = Get-Command conda -ErrorAction SilentlyContinue
if ($CondaCommand) {
    $Conda = $CondaCommand.Source
} else {
    $Conda = Join-Path $MinicondaDir "Scripts\conda.exe"
    $InstalledMiniconda = if (Test-Path $MinicondaMarker) {
        (Get-Content $MinicondaMarker -Raw).Trim()
    } else { "" }
    if ((Test-Path $Conda) -and $InstalledMiniconda -ne $MinicondaVersion) {
        Remove-Item $MinicondaDir -Recurse -Force
    }
    if (-not (Test-Path $Conda)) {
        Write-Host "Downloading local Miniconda bootstrap..."
        $Installer = Join-Path $BuildRoot "miniconda-installer.exe"
        $Downloaded = $false
        $MinicondaUrls = @(
            "https://mirrors.tuna.tsinghua.edu.cn/anaconda/miniconda/Miniconda3-py310_24.7.1-0-Windows-x86_64.exe",
            "https://repo.anaconda.com/miniconda/Miniconda3-py310_24.7.1-0-Windows-x86_64.exe"
        )
        foreach ($Url in $MinicondaUrls) {
            try {
                Write-Host "  Trying $Url"
                & curl.exe --location --fail --retry 3 --connect-timeout 30 `
                    --output $Installer $Url
                if ($LASTEXITCODE -ne 0) {
                    throw "curl exited with $LASTEXITCODE"
                }
                if ((Get-Item $Installer).Length -gt 10MB) {
                    $Downloaded = $true
                    break
                }
            } catch {
                Write-Warning "  Download failed: $($_.Exception.Message)"
                Remove-Item $Installer -Force -ErrorAction SilentlyContinue
            }
        }
        if (-not $Downloaded) {
            throw "Unable to download Miniconda from configured mirrors."
        }
        $Process = Start-Process -FilePath $Installer -Wait -PassThru `
            -ArgumentList @("/InstallationType=JustMe", "/RegisterPython=0",
                            "/AddToPath=0", "/S", "/D=$MinicondaDir")
        if ($Process.ExitCode -ne 0) {
            throw "Miniconda install failed: exit $($Process.ExitCode)"
        }
        Remove-Item $Installer -Force -ErrorAction SilentlyContinue
        Set-Content -Path $MinicondaMarker -Value $MinicondaVersion -Encoding ascii
    }
}

$EnvPython = Join-Path $EnvDir "python.exe"
if ($Force -and (Test-Path $EnvDir)) {
    Remove-Item $EnvDir -Recurse -Force
}
if (-not (Test-Path $EnvPython)) {
    Write-Host "Creating pinned Python 3.10 runtime environment..."
    & $Conda create -y -p $EnvDir python=3.10 pip `
        --override-channels `
        -c https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main `
        -c https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/msys2
    if ($LASTEXITCODE -ne 0) { throw "conda create failed" }
}

Write-Host "Installing the same CUDA/runtime dependencies used by CI..."
& $EnvPython -m pip install --upgrade "pip<25" "setuptools<81" wheel `
    -i https://pypi.tuna.tsinghua.edu.cn/simple `
    --trusted-host pypi.tuna.tsinghua.edu.cn
if ($LASTEXITCODE -ne 0) { throw "pip bootstrap failed" }
$CudaWheels = @(
    @{
        Name = "torch-2.0.1+cu118-cp310-cp310-win_amd64.whl"
        Url = "https://download.pytorch.org/whl/cu118/torch-2.0.1%2Bcu118-cp310-cp310-win_amd64.whl"
        Size = 2619146901
    },
    @{
        Name = "torchvision-0.15.2+cu118-cp310-cp310-win_amd64.whl"
        Url = "https://download.pytorch.org/whl/cu118/torchvision-0.15.2%2Bcu118-cp310-cp310-win_amd64.whl"
        Size = 4947556
    },
    @{
        Name = "torchaudio-2.0.2+cu118-cp310-cp310-win_amd64.whl"
        Url = "https://download.pytorch.org/whl/cu118/torchaudio-2.0.2%2Bcu118-cp310-cp310-win_amd64.whl"
        Size = 2459856
    }
)
$WheelPaths = @()
foreach ($Wheel in $CudaWheels) {
    $WheelPath = Join-Path $WheelDir $Wheel.Name
    $ExistingSize = if (Test-Path $WheelPath) {
        (Get-Item $WheelPath).Length
    } else { 0 }
    if ($ExistingSize -ne $Wheel.Size) {
        if ($ExistingSize -gt $Wheel.Size) {
            Remove-Item $WheelPath -Force
        }
        Write-Host "Downloading $($Wheel.Name) with resume support..."
        & curl.exe --location --fail --retry 20 --retry-all-errors `
            --retry-delay 2 --connect-timeout 30 --speed-limit 1024 `
            --speed-time 60 --continue-at - --output $WheelPath $Wheel.Url
        if ($LASTEXITCODE -ne 0) { throw "CUDA wheel download failed: $($Wheel.Name)" }
        if ((Get-Item $WheelPath).Length -ne $Wheel.Size) {
            throw "CUDA wheel size mismatch: $($Wheel.Name)"
        }
    } else {
        Write-Host "Using cached $($Wheel.Name)"
    }
    $WheelPaths += $WheelPath
}
& $EnvPython -m pip install @WheelPaths `
    -i https://pypi.tuna.tsinghua.edu.cn/simple `
    --trusted-host pypi.tuna.tsinghua.edu.cn
if ($LASTEXITCODE -ne 0) { throw "CUDA PyTorch install failed" }
& $EnvPython -m pip install Cython==0.29.36 `
    -i https://pypi.tuna.tsinghua.edu.cn/simple `
    --trusted-host pypi.tuna.tsinghua.edu.cn --timeout 300 --retries 10
if ($LASTEXITCODE -ne 0) { throw "Cython bootstrap install failed" }
& $EnvPython -m pip install --no-deps `
    basicsr==1.4.2 facexlib==0.3.0 gfpgan==1.3.8 `
    -i https://pypi.tuna.tsinghua.edu.cn/simple `
    --trusted-host pypi.tuna.tsinghua.edu.cn --timeout 300 --retries 10
if ($LASTEXITCODE -ne 0) { throw "Enhancer module bootstrap install failed" }
& $EnvPython -m pip install `
    -r (Join-Path $Root "packaging\sadtalker-runtime-requirements.txt") `
    -i https://pypi.tuna.tsinghua.edu.cn/simple `
    --trusted-host pypi.tuna.tsinghua.edu.cn --timeout 300 --retries 10
if ($LASTEXITCODE -ne 0) { throw "SadTalker dependency install failed" }

$CommitFile = Join-Path $SourceDir ".media-agent-source-commit"
$CurrentCommit = if (Test-Path $CommitFile) {
    (Get-Content $CommitFile -Raw).Trim()
} else { "" }
if ($CurrentCommit -ne $SourceCommit) {
    $GitCommand = Get-Command git -ErrorAction SilentlyContinue
    $Git = if ($GitCommand) { $GitCommand.Source } else {
        @(
            (Join-Path $env:ProgramFiles "Git\cmd\git.exe"),
            (Join-Path ${env:ProgramFiles(x86)} "Git\cmd\git.exe"),
            (Join-Path $env:LOCALAPPDATA "Programs\Git\cmd\git.exe")
        ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    }
    if (-not $Git) {
        throw "Git is required to prepare the SadTalker source."
    }
    Remove-Item $SourceDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Cloning pinned SadTalker source..."
    & $Git clone https://github.com/OpenTalker/SadTalker.git $SourceDir
    if ($LASTEXITCODE -ne 0) { throw "SadTalker clone failed" }
    & $Git -C $SourceDir checkout $SourceCommit
    if ($LASTEXITCODE -ne 0) { throw "SadTalker checkout failed" }
    Remove-Item (Join-Path $SourceDir ".git") -Recurse -Force
    Set-Content -Path $CommitFile -Value $SourceCommit -Encoding ascii
}

$SitePackages = Join-Path $EnvDir "Lib\site-packages"
Set-Content -Path (Join-Path $SitePackages "sadtalker-source.pth") `
    -Value $SourceDir -Encoding ascii

& $EnvPython -c "import cv2, torch, torchvision, face_alignment, safetensors; print(torch.__version__)"
if ($LASTEXITCODE -ne 0) { throw "SadTalker runtime import check failed" }
& $EnvPython (Join-Path $SourceDir "inference.py") --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "SadTalker source import check failed" }

Remove-Item (Join-Path $ArtifactDir "sadtalker-runtime-win64-cuda118*") `
    -Force -ErrorAction SilentlyContinue
Write-Host "Packing runtime in the release-compatible format..."
& $EnvPython (Join-Path $Root "packaging\pack_sadtalker_runtime.py") `
    --prefix $EnvDir --output-dir $ArtifactDir --keep-archive
if ($LASTEXITCODE -ne 0) { throw "Runtime packing failed" }

$Archive = Join-Path $ArtifactDir "sadtalker-runtime-win64-cuda118.tar.gz"
$ProjectPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $ProjectPython)) { $ProjectPython = "python" }
$PrepareArgs = @(
    (Join-Path $Root "packaging\prepare_sadtalker_dev.py"),
    "--archive", $Archive,
    "--data-dir", $DataDir
)
if ($Proxy) { $PrepareArgs += @("--proxy", $Proxy) }

Write-Host "Installing runtime and models into $DataDir ..."
& $ProjectPython @PrepareArgs
if ($LASTEXITCODE -ne 0) { throw "Managed runtime/model preparation failed" }

Write-Host ""
Write-Host "SadTalker development environment prepared."
Write-Host "Runtime: $(Join-Path $DataDir 'runtimes\sadtalker')"
Write-Host "Models:  $(Join-Path $DataDir 'models\sadtalker')"
