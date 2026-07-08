<#
  Jarvis installer (Windows) — pulls Jarvis into an ISOLATED, auto-managed environment via pipx
  and exposes a global `jarvis` command. No hand-built venv; run once, then `cd` into any project
  and type `jarvis`.

  Usage (from the repo root, after `git clone` / pull):
      .\install.ps1                       # full install: voice + free Kokoro TTS + dashboard + Telegram + web
      .\install.ps1 -Gpu                  # + onnxruntime-gpu (run Kokoro/whisper on an NVIDIA GPU)
      .\install.ps1 -Clone                # + XTTS-v2 voice clone (coqui-tts + a CUDA torch build)
      .\install.ps1 -Extras "voice,dashboard"   # pick your own extras instead of the default bundle
      .\install.ps1 -Python "C:\Python311\python.exe"   # use a specific Python (3.11 recommended for -Clone)

  Re-run any time to upgrade (it force-reinstalls). Uninstall with:  pipx uninstall jarvis
#>
[CmdletBinding()]
param(
    [string]$Extras = "all",     # pip extras to install ("all" = voice,kokoro,dashboard,presence,web)
    [switch]$Gpu,                # inject onnxruntime-gpu so Kokoro/whisper can use CUDA
    [switch]$Clone,              # inject coqui-tts + a CUDA torch build (XTTS-v2 voice clone)
    [string]$Python = "",        # explicit python.exe for the venv (3.11 recommended with -Clone)
    [string]$Cuda = "cu121"      # torch CUDA wheel tag for -Clone (match your driver: cu118/cu121/cu124)
)

$ErrorActionPreference = "Stop"
$RepoPath = Split-Path -Parent $MyInvocation.MyCommand.Definition

function Info($m)  { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)    { Write-Host "  * $m" -ForegroundColor Green }
function Warn($m)  { Write-Host "  ! $m" -ForegroundColor Yellow }

# 1. Find a Python interpreter -------------------------------------------------------------
function Resolve-Python {
    if ($Python) {
        if (Test-Path $Python) { return $Python }
        throw "The -Python path '$Python' does not exist."
    }
    $cand = (Get-Command python -ErrorAction SilentlyContinue)
    if ($cand) { return $cand.Source }
    $pyLauncher = (Get-Command py -ErrorAction SilentlyContinue)
    if ($pyLauncher) { return "py" }   # the Windows launcher; we'll call `py -3`
    throw "No Python found. Install Python 3.10+ from https://python.org and re-run."
}
$Py = Resolve-Python
if ($Py -eq "py") { $PyArgs = @("-3") } else { $PyArgs = @() }
$PyVer = (& $Py @PyArgs --version) 2>&1
Info "Using Python: $Py  ($PyVer)"

# 2. Ensure pipx (the isolated-app installer) ----------------------------------------------
$havePipx = $true
try { & $Py @PyArgs -m pipx --version | Out-Null } catch { $havePipx = $false }
if (-not $havePipx) {
    Info "Installing pipx (one-time)…"
    & $Py @PyArgs -m pip install --user --upgrade pipx
    & $Py @PyArgs -m pipx ensurepath | Out-Null
    Ok "pipx installed (a new terminal will have `pipx` on PATH; this run doesn't need it)."
} else {
    Ok "pipx already present."
}

# 3. Install Jarvis from THIS repo into its own environment ---------------------------------
$spec = "$RepoPath[$Extras]"
Info "Installing jarvis[$Extras] from $RepoPath …"
$installArgs = @("-m", "pipx", "install", "--force", $spec)
if ($Python) { $installArgs += @("--python", $Python) }
& $Py @PyArgs @installArgs
Ok "jarvis installed."

# 4. Optional: GPU acceleration for Kokoro / faster-whisper --------------------------------
if ($Gpu) {
    Info "Injecting onnxruntime-gpu (Kokoro on CUDA)…"
    & $Py @PyArgs -m pipx inject jarvis onnxruntime-gpu
    Warn "faster-whisper on GPU also needs CUDA + cuDNN on PATH (nvidia-cublas-cu12 / nvidia-cudnn-cu12)."
    Ok "GPU runtime injected. Set whisper_device/kokoro_device = \"cuda\" in .jarvis/config.toml."
}

# 5. Optional: XTTS-v2 voice clone (heavy; torch) ------------------------------------------
if ($Clone) {
    Info "Injecting the voice-clone engine (coqui-tts) + a CUDA torch build ($Cuda)…"
    if (-not $Python) { Warn "coqui-tts is happiest on Python 3.11 — if this fails, re-run with -Python <py3.11>." }
    & $Py @PyArgs -m pipx inject jarvis coqui-tts
    & $Py @PyArgs -m pipx runpip jarvis install torch torchaudio --index-url "https://download.pytorch.org/whl/$Cuda"
    Ok "Voice clone ready. Set tts_engine=\"xtts\" + xtts_reference in .jarvis/config.toml (see config.voice-clone.example.toml)."
}

# 6. Check the Claude CLI (the brain runs on your subscription through it) ------------------
$claude = Get-Command claude -ErrorAction SilentlyContinue
if ($claude) {
    Ok "Claude CLI found at $($claude.Source) — the brain uses your subscription via it."
} else {
    Warn "Claude CLI not found. The brain needs it (subscription auth). Install + log in:"
    Warn "    npm install -g @anthropic-ai/claude-code"
    Warn "    claude    (sign in once)   — or:   claude setup-token"
}
if ($env:ANTHROPIC_API_KEY) {
    Warn "ANTHROPIC_API_KEY is set in your environment. Jarvis unsets it per-run so the brain uses"
    Warn "your subscription, but consider removing it to avoid surprises."
}

# 7. Done ----------------------------------------------------------------------------------
Write-Host ""
Ok "Installed. Next:"
Write-Host "    cd <any project>"
Write-Host "    jarvis --init      # optional: scaffold .jarvis/ (config + .env.example)"
Write-Host "    jarvis             # text REPL   |   jarvis --voice   |   jarvis --remote"
Write-Host ""
Write-Host "  Dashboard from another machine (Jarvis stays here, browser is elsewhere):"
Write-Host "    ssh -L 8765:localhost:8765 <this-host>   then open http://localhost:8765/"
