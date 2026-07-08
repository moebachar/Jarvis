"""Per-machine profile: detect the GPU and write ``~/.jarvis/machine.toml``.

This file is installer-owned — declared once per box so BOTH the installer (which extras to
install) and the runtime (whether to use CUDA) read the same source of truth. `jarvis
--machine-init` calls `write_machine_profile`; the installer writes the same format directly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .config import machine_toml_path

_TEMPLATE = """\
# Jarvis - per-machine profile. Written by the installer / `jarvis --machine-init`.
# Declares THIS box's hardware once; both install and runtime read it, so you never pass GPU
# flags or hand-edit device fields per machine. Re-run `jarvis --machine-init` to refresh it.
[machine]
gpu         = {gpu}   # this machine has an NVIDIA GPU (runtime uses CUDA for STT + TTS)
voice_clone = {clone}   # the XTTS-v2 voice clone is installed here
cuda        = "{cuda}"   # torch CUDA wheel tag used for the clone build (cu118 | cu121 | cu124)
extras      = "{extras}"   # pip extras the installer installs
python      = "{python}"   # explicit python for the venv ("" = auto; 3.11 suits the clone)
"""


def detect_gpu() -> bool:
    """True if an NVIDIA GPU looks present (``nvidia-smi`` exists and exits cleanly)."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return False
    try:
        return subprocess.run(
            [exe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10
        ).returncode == 0
    except Exception:
        return False


def write_machine_profile(
    *,
    gpu: bool,
    voice_clone: bool = False,
    cuda: str = "cu121",
    python: str = "",
    extras: str = "all",
) -> Path:
    """Write ``~/.jarvis/machine.toml`` with this machine's profile and return the path.

    The file is fully owned by the installer/this function, so it's safe to overwrite whole.
    """
    path = machine_toml_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _TEMPLATE.format(
        gpu=str(bool(gpu)).lower(),
        clone=str(bool(voice_clone)).lower(),
        cuda=cuda,
        extras=extras,
        python=python,
    )
    path.write_text(text, encoding="utf-8")
    return path


def resolve_gpu(flag_gpu: bool | None) -> bool:
    """Decide GPU-ness: an explicit flag wins; otherwise auto-detect via nvidia-smi."""
    if flag_gpu is not None:
        return flag_gpu
    return detect_gpu()


def _read_profile() -> dict:
    """The existing ``[machine]`` table (or {} if none), for the installer's resolution step."""
    import tomllib

    path = machine_toml_path()
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh).get("machine", {}) or {}
    except Exception:
        return {}


def resolve_profile(env: dict) -> dict:
    """Resolve this machine's profile for the INSTALLER: forced env flags > existing
    machine.toml > auto-detect. `env` is os.environ (JARVIS_FORCE_* keys). Pure given `env` +
    the file + detect_gpu(), so the installer never re-implements the precedence rules.
    """
    prof = _read_profile()

    def pick_bool(name, cur, auto):
        v = env.get(name)
        if v is not None:
            return v == "1"
        if cur is not None:
            return bool(cur)
        return auto

    return {
        "GPU": pick_bool("JARVIS_FORCE_GPU", prof.get("gpu"), detect_gpu()),
        "CLONE": pick_bool("JARVIS_FORCE_CLONE", prof.get("voice_clone"), False),
        "CUDA": env.get("JARVIS_FORCE_CUDA") or prof.get("cuda") or "cu121",
        "EXTRAS": env.get("JARVIS_FORCE_EXTRAS") or prof.get("extras") or "all",
        "PYTHON": env.get("JARVIS_FORCE_PYTHON") or prof.get("python") or "",
    }


if __name__ == "__main__":
    # For install.ps1 / install.sh: resolve this machine's profile, PERSIST it to
    # ~/.jarvis/machine.toml (so it doesn't depend on the freshly-installed `jarvis` being on
    # PATH yet), and print it as KEY=VALUE lines for the shell to parse.
    import os

    r = resolve_profile(os.environ)
    write_machine_profile(
        gpu=r["GPU"], voice_clone=r["CLONE"], cuda=r["CUDA"],
        python=r["PYTHON"], extras=r["EXTRAS"],
    )
    print(f"GPU={'1' if r['GPU'] else '0'}")
    print(f"CLONE={'1' if r['CLONE'] else '0'}")
    print(f"CUDA={r['CUDA']}")
    print(f"EXTRAS={r['EXTRAS']}")
    print(f"PYTHON={r['PYTHON']}")
