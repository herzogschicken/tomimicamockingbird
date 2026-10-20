"""GPU helpers (NVIDIA only, Linux and Windows).

ctranslate2 (the engine under faster-whisper) loads cuBLAS for CUDA 12 at runtime:
  Linux:   libcublas.so.12      Windows: cublas64_12.dll
It comes from the pip package nvidia-cublas-cu12 inside the venv, so no CUDA toolkit is
needed. Only the NVIDIA driver must be installed. Newer drivers run these CUDA 12 libs fine.

Every GPU job runs in a subprocess. If a library is missing, ctranslate2 can abort the
whole process, and that must never take the web server down.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import IS_WINDOWS

NVIDIA_PKGS = ["nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_runtime", "nvidia.cuda_nvrtc"]

if IS_WINDOWS:
    DRIVER_LIB, CUDA_LIBS = "nvcuda.dll", ["cublas64_12.dll", "cublasLt64_12.dll"]
else:
    DRIVER_LIB, CUDA_LIBS = "libcuda.so.1", ["libcublas.so.12", "libcublasLt.so.12"]


def nvidia_lib_dirs():
    """Folders holding the pip-installed NVIDIA libraries (lib/ on Linux, bin/ on Windows)."""
    dirs = []
    for name in NVIDIA_PKGS:
        try:
            spec = importlib.util.find_spec(name)
        except (ModuleNotFoundError, ValueError):
            spec = None
        if spec and spec.submodule_search_locations:
            for loc in spec.submodule_search_locations:
                for sub in ("bin", "lib"):
                    d = Path(loc) / sub
                    if d.is_dir():
                        dirs.append(str(d))
    return dirs


def has_nvidia_gpu():
    """True if an NVIDIA driver seems to be installed (nvidia-smi present)."""
    return shutil.which("nvidia-smi") is not None


def child_env():
    """Environment for GPU subprocesses: pip-installed CUDA libs on the library search path."""
    env = dict(os.environ)
    extra = nvidia_lib_dirs()
    key = "PATH" if IS_WINDOWS else "LD_LIBRARY_PATH"
    if extra:
        env[key] = os.pathsep.join(extra + [p for p in env.get(key, "").split(os.pathsep) if p])
    env.setdefault("PYTHONUNBUFFERED", "1")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def add_dll_dirs():
    """Windows only: let this process load the pip-installed CUDA DLLs."""
    if IS_WINDOWS and hasattr(os, "add_dll_directory"):
        for d in nvidia_lib_dirs():
            try:
                os.add_dll_directory(d)
            except OSError:
                pass


PROBE_CODE = r"""
import ctypes, json, os, sys
r = {"ok": False, "libs": {}, "cuda_devices": 0}
libs = json.loads(sys.argv[1])
dirs = json.loads(sys.argv[2])
if os.name == "nt" and hasattr(os, "add_dll_directory"):
    for d in dirs:
        try: os.add_dll_directory(d)
        except OSError: pass
for lib in libs:
    try:
        ctypes.CDLL(lib); r["libs"][lib] = True
    except OSError:
        r["libs"][lib] = False
try:
    import ctranslate2
    r["ctranslate2"] = ctranslate2.__version__
    r["cuda_devices"] = ctranslate2.get_cuda_device_count()
except Exception as e:
    r["error"] = f"ctranslate2: {e}"
try:
    import faster_whisper
    r["faster_whisper"] = faster_whisper.__version__
except Exception as e:
    r["error"] = f"faster_whisper: {e}"
r["ok"] = r["cuda_devices"] > 0 and all(r["libs"].values()) and "error" not in r
print(json.dumps(r))
"""


def probe(timeout=60):
    """Cheap check: is a CUDA GPU visible and can the CUDA 12 libs load?"""
    if not has_nvidia_gpu():
        return {"ok": False, "no_gpu": True,
                "hint": "No NVIDIA GPU found. Transcription runs on the CPU (slower, but it works)."}
    try:
        res = subprocess.run(
            [sys.executable, "-c", PROBE_CODE, json.dumps([DRIVER_LIB] + CUDA_LIBS), json.dumps(nvidia_lib_dirs())],
            env=child_env(), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        out = res.stdout.strip().splitlines()
        data = json.loads(out[-1]) if out else {"ok": False, "error": res.stderr.strip()[-400:]}
    except Exception as e:
        data = {"ok": False, "error": str(e)}
    missing = [k for k, v in data.get("libs", {}).items() if not v]
    if not data.get("ok"):
        if DRIVER_LIB in missing:
            data["hint"] = "The NVIDIA driver is not loaded. Check that the nvidia-smi command works."
        elif missing:
            data["hint"] = ("The GPU library (cuBLAS) is missing. Run the start script again; it installs "
                            "it when it finds an NVIDIA card.")
        elif data.get("cuda_devices", 0) == 0 and "error" not in data:
            data["hint"] = "No CUDA GPU visible to the transcription engine."
    return data
