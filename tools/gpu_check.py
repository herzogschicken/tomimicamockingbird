#!/usr/bin/env python3
"""Check that transcription can use your NVIDIA graphics card.

    Linux:    .venv/bin/python tools/gpu_check.py
    Windows:  .venv\\Scripts\\python tools\\gpu_check.py

Add a model name to test the real one, e.g. `tools/gpu_check.py large-v3-turbo` (1.6 GB download).
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import gpu  # noqa: E402

# The CUDA libraries must be on the search path when the process starts, so run the
# real test in a child process with the right environment.
if os.environ.get("TMM_GPU_CHECK_CHILD") != "1":
    env = gpu.child_env()
    env["TMM_GPU_CHECK_CHILD"] = "1"
    sys.exit(subprocess.call([sys.executable] + sys.argv, env=env))

import numpy as np  # noqa: E402

from app.config import DATA_DIR  # noqa: E402

model_name = sys.argv[1] if len(sys.argv) > 1 else "tiny"

print("1. Looking for the graphics card")
p = gpu.probe()
for k, v in p.items():
    print(f"   {k}: {v}")
if not p.get("ok"):
    if p.get("no_gpu"):
        print("\nNo NVIDIA card found. That's fine: the app will use the CPU. Nothing else to do.")
        sys.exit(0)
    sys.exit("\nThe card is there but can't be used yet. Read the hint above.")

print(f"2. Loading the {model_name} model on the graphics card")
gpu.add_dll_dirs()
from faster_whisper import WhisperModel  # noqa: E402

m = WhisperModel(model_name, device="cuda", compute_type="float16", download_root=str(DATA_DIR / "models"))

print("3. Test run on 10 seconds of sound")
sr = 16000
x = np.arange(sr * 10) / sr
audio = (0.1 * np.sin(2 * np.pi * 220 * x)).astype(np.float32)
segs, _ = m.transcribe(audio, word_timestamps=True, vad_filter=False)
list(segs)
print("\nGPU OK. Transcription will use your graphics card.")
