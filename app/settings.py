import json

from . import db

DEFAULTS = {
    "model": "auto",              # auto = large-v3-turbo on a GPU, small on a CPU
    "compute_type": "float16",    # float16 | int8_float16 (GPU) ; CPU always uses int8
    "device": "auto",             # auto | cuda | cpu
    "language": "",               # "" = detect from the first chapter, else e.g. "en"
    "beam_size": 5,
    "batched": True,              # process many audio chunks on the GPU at once (much faster)
    "batch_size": 16,
    "sync_mode": "auto",          # auto (phone if reachable, else manual clock) | phone | manual
    "adb_serial": "",             # pick a device when several are connected
    "adb_address": "",            # wireless adb host:port, e.g. over Tailscale
    "player_package": "",         # "" = Smart Audiobook Player if present, else whichever app is playing
    "phone_folders": "",          # comma-separated phone folders to search; empty = whole storage
    "delete_audio_after": True,   # imported audio is deleted from the PC once transcribed
    "auto_transcribe": True,      # start transcribing as soon as an import finishes
}

MODELS = ["auto", "large-v3-turbo", "large-v3", "distil-large-v3", "medium", "small", "base"]
COMPUTE_TYPES = ["float16", "int8_float16"]
DEVICES = ["auto", "cuda", "cpu"]
SYNC_MODES = ["auto", "phone", "manual"]


def get():
    with db.db() as con:
        row = con.execute("SELECT value FROM app_state WHERE key='settings'").fetchone()
    saved = json.loads(row["value"]) if row else {}
    # Settings saved before batched mode existed stored every value, including the old default
    # model. Treat that stored "large-v3" as "not chosen" so the new turbo default applies.
    if "batched" not in saved and saved.get("model") == "large-v3":
        saved.pop("model")
    # Before "auto" existed, turbo was the stored default. Keep a deliberate choice, but a
    # setup that never touched the model gets "auto" (so CPU-only machines use a small model).
    if saved.get("model") == "large-v3-turbo" and not saved.get("model_chosen"):
        saved.pop("model")
    out = {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}
    out["model_chosen"] = bool(saved.get("model_chosen"))
    return out


def update(changes):
    cur = get()
    for k, v in changes.items():
        if k not in DEFAULTS:
            continue
        if k == "model" and v not in MODELS:
            raise ValueError(f"Unknown model {v}")
        if k == "model":
            cur["model_chosen"] = True
        if k == "compute_type" and v not in COMPUTE_TYPES:
            raise ValueError(f"Unknown compute type {v}")
        if k == "device" and v not in DEVICES:
            raise ValueError(f"Unknown device {v}")
        if k == "sync_mode" and v not in SYNC_MODES:
            raise ValueError(f"Unknown sync mode {v}")
        if k in ("delete_audio_after", "auto_transcribe"):
            v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on", "yes")
        if k in ("adb_serial", "adb_address", "phone_folders", "player_package"):
            v = (v or "").strip()
        if k == "language":
            v = (v or "").strip().lower()
        if k == "beam_size":
            v = max(1, min(10, int(v)))
        if k == "batch_size":
            v = max(1, min(32, int(v)))
        if k == "batched":
            v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on", "yes")
        cur[k] = v
    with db.db() as con:
        con.execute("INSERT INTO app_state (key,value) VALUES ('settings',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(cur),))
    return cur
