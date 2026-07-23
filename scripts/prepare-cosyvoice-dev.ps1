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
$EnvDir = Join-Path $BuildRoot "cosyvoice-env"
$ArtifactDir = Join-Path $BuildRoot "cosyvoice-artifacts"
$SourceDir = Join-Path $EnvDir "cosyvoice-src"
$SourceCommit = "074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc"
$MinicondaVersion = "py310_24.7.1-0"
$MinicondaMarker = Join-Path $MinicondaDir ".media-agent-version"
New-Item -ItemType Directory -Force -Path $BuildRoot, $ArtifactDir | Out-Null

$CondaCommand = Get-Command conda -ErrorAction SilentlyContinue
if ($CondaCommand) {
    $Conda = $CondaCommand.Source
} else {
    $Conda = Join-Path $MinicondaDir "Scripts\conda.exe"
    $InstalledVersion = if (Test-Path $MinicondaMarker) {
        (Get-Content $MinicondaMarker -Raw).Trim()
    } else { "" }
    if ((Test-Path $Conda) -and $InstalledVersion -ne $MinicondaVersion) {
        Remove-Item $MinicondaDir -Recurse -Force
    }
    if (-not (Test-Path $Conda)) {
        $Installer = Join-Path $BuildRoot "miniconda-installer.exe"
        Write-Host "Downloading local Miniconda bootstrap..."
        & curl.exe --location --fail --retry 5 --continue-at - `
            --output $Installer `
            "https://repo.anaconda.com/miniconda/Miniconda3-py310_24.7.1-0-Windows-x86_64.exe"
        if ($LASTEXITCODE -ne 0) { throw "Miniconda download failed" }
        $Process = Start-Process -FilePath $Installer -Wait -PassThru `
            -ArgumentList @("/InstallationType=JustMe", "/RegisterPython=0",
                            "/AddToPath=0", "/S", "/D=$MinicondaDir")
        if ($Process.ExitCode -ne 0) { throw "Miniconda install failed" }
        Set-Content $MinicondaMarker $MinicondaVersion -Encoding ascii
    }
}

if ($Force -and (Test-Path $EnvDir)) {
    Remove-Item $EnvDir -Recurse -Force
}
$EnvPython = Join-Path $EnvDir "python.exe"
if (-not (Test-Path $EnvPython)) {
    & $Conda create -y -p $EnvDir python=3.10 pip
    if ($LASTEXITCODE -ne 0) { throw "conda create failed" }
}

Write-Host "Installing release-pinned CUDA and CosyVoice dependencies..."
& $EnvPython -m pip install --upgrade "pip<25" "setuptools<81" wheel
& $EnvPython -m pip install torch==2.3.1 torchaudio==2.3.1 `
    --index-url https://download.pytorch.org/whl/cu121 `
    --timeout 300 --retries 10
if ($LASTEXITCODE -ne 0) { throw "CUDA PyTorch install failed" }
& $EnvPython -m pip install `
    -r (Join-Path $Root "packaging\cosyvoice-runtime-requirements.txt") `
    --timeout 300 --retries 10
if ($LASTEXITCODE -ne 0) { throw "CosyVoice dependency install failed" }

$CommitFile = Join-Path $SourceDir ".media-agent-source-commit"
$CurrentCommit = if (Test-Path $CommitFile) {
    (Get-Content $CommitFile -Raw).Trim()
} else { "" }
if ($CurrentCommit -ne $SourceCommit) {
    Remove-Item $SourceDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Cloning pinned CosyVoice source and submodules..."
    git clone --recursive https://github.com/FunAudioLLM/CosyVoice.git $SourceDir
    if ($LASTEXITCODE -ne 0) { throw "CosyVoice clone failed" }
    git -C $SourceDir checkout $SourceCommit
    git -C $SourceDir submodule update --init --recursive
    if ($LASTEXITCODE -ne 0) { throw "CosyVoice checkout failed" }
    Remove-Item (Join-Path $SourceDir ".git") -Recurse -Force
    Set-Content $CommitFile $SourceCommit -Encoding ascii
}
$SitePackages = Join-Path $EnvDir "Lib\site-packages"
Set-Content (Join-Path $SitePackages "cosyvoice-source.pth") `
    $SourceDir -Encoding ascii

$env:PYTHONPATH = $SourceDir
& $EnvPython -c "import torch, onnxruntime, whisper; from cosyvoice.cli.cosyvoice import CosyVoice2; assert torch.version.cuda; print(torch.__version__, torch.version.cuda)"
if ($LASTEXITCODE -ne 0) { throw "CosyVoice runtime import check failed" }

Remove-Item (Join-Path $ArtifactDir "cosyvoice-runtime-win64-cuda128*") `
    -Force -ErrorAction SilentlyContinue
& $EnvPython (Join-Path $Root "packaging\pack_cosyvoice_runtime.py") `
    --prefix $EnvDir --output-dir $ArtifactDir --keep-archive
if ($LASTEXITCODE -ne 0) { throw "Runtime packing failed" }

$Archive = Join-Path $ArtifactDir "cosyvoice-runtime-win64-cuda128.tar.gz"
$ProjectPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $ProjectPython)) { $ProjectPython = "python" }
$Args = @(
    (Join-Path $Root "packaging\prepare_cosyvoice_dev.py"),
    "--archive", $Archive, "--data-dir", $DataDir
)
if ($Proxy) { $Args += @("--proxy", $Proxy) }
& $ProjectPython @Args
if ($LASTEXITCODE -ne 0) { throw "Managed runtime/model preparation failed" }

Write-Host "CosyVoice development runtime prepared."
Write-Host "Runtime: $(Join-Path $DataDir 'runtimes\cosyvoice')"
Write-Host "Models:  $(Join-Path $DataDir 'models\cosyvoice\CosyVoice2-0.5B')"
