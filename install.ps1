<#
  Jarvis installer (Windows) - pulls Jarvis into an ISOLATED, auto-managed environment via pipx
  and exposes a global `jarvis` command. No hand-built venv; run once, then `cd` into any project
  and type `jarvis`.

  It reads this machine's profile (~/.jarvis/machine.toml) - or auto-detects your GPU with
  nvidia-smi the first time - so you DON'T pass flags per machine. Just:

      .\install.ps1                 # detects GPU/clone from the machine profile (or auto-detects)

  Override the profile for this run (also saved back to the profile):
      .\install.ps1 -Gpu            # force GPU support on   (-NoGpu forces it off)
      .\install.ps1 -Clone          # also install the XTTS-v2 voice clone (coqui-tts + CUDA torch)
      .\install.ps1 -Extras "voice,dashboard"           # custom pip extras instead of the bundle
      .\install.ps1 -Python "C:\Python311\python.exe"   # specific Python (3.11 suits -Clone)

  Re-run any time to upgrade. Uninstall with:  pipx uninstall jarvis
#>
[CmdletBinding()]
param(
    [switch]$Gpu,                # force GPU support ON  (overrides the machine profile + autodetect)
    [switch]$NoGpu,              # force GPU support OFF
    [switch]$Clone,             # also install the XTTS-v2 voice clone (coqui-tts + a CUDA torch build)
    [string]$Extras = "",        # pip extras ("" = from profile, default "all")
    [string]$Python = "",        # explicit python.exe for the venv (3.11 suits -Clone)
    [string]$Cuda = ""           # torch CUDA wheel tag for -Clone ("" = from profile, default cu121)
)

$ErrorActionPreference = "Stop"
$RepoPath = Split-Path -Parent $MyInvocation.MyCommand.Definition

function Info($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  * $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  ! $m" -ForegroundColor Yellow }

# Run pipx via the chosen Python and FAIL LOUDLY on a non-zero exit. (A failing native command
# does NOT throw in PowerShell, so we must check $LASTEXITCODE ourselves - otherwise a broken
# step is silently reported as success.)
function Invoke-Pipx {
    param([Parameter(Mandatory, ValueFromRemainingArguments = $true)] [string[]] $PipxArgs)
    & $Py @PyArgs -m pipx @PipxArgs
    if ($LASTEXITCODE -ne 0) {
        throw "pipx $($PipxArgs -join ' ') failed (exit $LASTEXITCODE). See the output above."
    }
}

# 1. Find a Python interpreter -------------------------------------------------------------
function Resolve-Python {
    if ($Python) {
        if (Test-Path $Python) { return $Python }
        throw "The -Python path '$Python' does not exist."
    }
    $cand = (Get-Command python -ErrorAction SilentlyContinue)
    if ($cand) { return $cand.Source }
    if (Get-Command py -ErrorAction SilentlyContinue) { return "py" }
    throw "No Python found. Install Python 3.10+ from https://python.org and re-run."
}
$Py = Resolve-Python
if ($Py -eq "py") { $PyArgs = @("-3") } else { $PyArgs = @() }
$pyVer = (& $Py @PyArgs --version 2>&1)
Info "Using Python: $Py  ($pyVer)"

# 2. Resolve THIS machine's profile (forced flags > machine.toml > auto-detect) ------------
# The precedence + persistence live in Python (jarvis.machine); we just pass forced flags via env.
if ($Gpu -and $NoGpu) { throw "Pass only one of -Gpu / -NoGpu." }
if ($Gpu)    { $env:JARVIS_FORCE_GPU = "1" }
if ($NoGpu)  { $env:JARVIS_FORCE_GPU = "0" }
if ($Clone)  { $env:JARVIS_FORCE_CLONE = "1" }
if ($Extras) { $env:JARVIS_FORCE_EXTRAS = $Extras }
if ($Python) { $env:JARVIS_FORCE_PYTHON = $Python }
if ($Cuda)   { $env:JARVIS_FORCE_CUDA = $Cuda }

Info "Resolving machine profile..."
Push-Location $RepoPath
try { $resolvedLines = & $Py @PyArgs -m jarvis.machine } finally { Pop-Location }
$P = @{}
foreach ($line in $resolvedLines) { if ($line -match '^(\w+)=(.*)$') { $P[$matches[1]] = $matches[2] } }
$useGpu    = ($P["GPU"]   -eq "1")
$useClone  = ($P["CLONE"] -eq "1")
$useCuda   = $P["CUDA"]
$useExtras = $P["EXTRAS"]
$usePython = $P["PYTHON"]
Ok "Profile: GPU=$useGpu  clone=$useClone  extras=$useExtras  cuda=$useCuda"

# 3. Ensure pipx (the isolated-app installer) ----------------------------------------------
# Detect pipx by asking Python if the module exists, then check the EXIT CODE (a failing
# native command doesn't throw, so try/catch would give a false "already present").
& $Py @PyArgs -c "import importlib.util as u, sys; sys.exit(0 if u.find_spec('pipx') else 1)"
$havePipx = ($LASTEXITCODE -eq 0)
if (-not $havePipx) {
    Info "Installing pipx (one-time)..."
    # `pip install --user` fails inside an active virtualenv; only pass --user outside one.
    if ($env:VIRTUAL_ENV) {
        Warn "A virtualenv is active ($env:VIRTUAL_ENV); installing pipx into it (that's fine - pipx"
        Warn "  still puts the global jarvis command on your PATH)."
        & $Py @PyArgs -m pip install --upgrade pipx
    } else {
        & $Py @PyArgs -m pip install --user --upgrade pipx
    }
    if ($LASTEXITCODE -ne 0) { throw "Failed to install pipx (pip exit $LASTEXITCODE). See the output above." }
    & $Py @PyArgs -m pipx ensurepath | Out-Null
    Ok "pipx installed (a new terminal will have it on PATH; this run calls it via python -m)."
} else {
    Ok "pipx already present."
}

# 4. Install Jarvis from THIS repo into its own environment --------------------------------
Info "Installing jarvis[$useExtras] from $RepoPath ..."
$spec = "$RepoPath[$useExtras]"
$installArgs = @("install", "--force", $spec)
if ($usePython) { $installArgs += @("--python", $usePython) }
Invoke-Pipx @installArgs
Ok "jarvis installed. (Machine profile saved to ~/.jarvis/machine.toml.)"

# 5. GPU acceleration (Kokoro on CUDA + faster-whisper CUDA libs) -------------------------
if ($useGpu) {
    Info "Injecting GPU runtimes (onnxruntime-gpu for Kokoro; cuBLAS + cuDNN for faster-whisper)..."
    Invoke-Pipx inject jarvis onnxruntime-gpu nvidia-cublas-cu12 nvidia-cudnn-cu12
    Ok "GPU runtimes injected. (If STT still can't find CUDA, Jarvis auto-falls back to CPU.)"
}

# 6. XTTS-v2 voice clone (heavy; torch) ---------------------------------------------------
if ($useClone) {
    Info "Injecting the voice-clone engine (coqui-tts) + a CUDA torch build ($useCuda)..."
    if (-not $usePython) { Warn "coqui-tts prefers Python 3.11; if this fails, re-run with -Python pointing at a Python 3.11." }
    Invoke-Pipx inject jarvis coqui-tts
    $torchIndex = "https://download.pytorch.org/whl/$useCuda"
    Invoke-Pipx runpip jarvis install torch torchaudio --index-url $torchIndex
}

# 7. Check the Claude CLI (the brain runs on your subscription through it) -----------------
if (Get-Command claude -ErrorAction SilentlyContinue) {
    Ok "Claude CLI found - the brain uses your subscription via it."
} else {
    Warn "Claude CLI not found. The brain needs it (subscription auth). Install + log in:"
    Warn "    npm install -g @anthropic-ai/claude-code   ;   claude   (sign in once)"
}
if ($env:ANTHROPIC_API_KEY) { Warn "ANTHROPIC_API_KEY is set; Jarvis unsets it per-run, but consider removing it." }

# 8. Done ----------------------------------------------------------------------------------
$runtimeDev = "CPU"
if ($useGpu) { $runtimeDev = "CUDA" }
# Is `jarvis` already on PATH in THIS session? (pipx ensurepath only affects NEW terminals.)
$onPath = [bool](Get-Command jarvis -ErrorAction SilentlyContinue)
Write-Host ""
Ok "Installed. Runtime will use $runtimeDev on this machine."
if (-not $onPath) {
    Warn "IMPORTANT: open a NEW terminal (or reopen the VS Code terminal) so the 'jarvis' command"
    Warn "  is on PATH. It won't be found in THIS window."
}
Write-Host ""
Write-Host "  Then:"
Write-Host "    cd <any project>"
Write-Host "    jarvis --init      # optional: scaffold .jarvis/ (config + .env.example)"
Write-Host "    jarvis             # text REPL   |   jarvis --voice   |   jarvis --remote"
Write-Host ""
Write-Host "  Dashboard from another machine (Jarvis stays here, browser elsewhere):"
Write-Host "    ssh -L 8765:localhost:8765 <this-host>   then open http://localhost:8765/"
