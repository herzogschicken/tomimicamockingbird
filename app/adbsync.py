"""Read an Android audio player's playback position over adb (tested with Smart Audiobook Player).

One adb call per sample returns both the phone's uptime and the media session dump, so the
position can be brought up to the moment of sampling:

    live = position + (uptime - updated) * speed     (only while playing)

Smart Audiobook Player reports whole-file time and refreshes it about every 12 s. The `updated`
stamp makes the estimate exact between refreshes, and pause, seek and speed changes show up
on the next poll.
"""
import re
import shutil
import subprocess
import threading
import time

SAP_PKG = "ak.alizandro.smartaudiobookplayer"

STATE_RE = re.compile(
    r"state=PlaybackState \{state=(?:\w+\()?(\d+)\)?,\s*position=(-?\d+),"
    r".*?speed=([-\d.]+),\s*updated=(\d+)")
PKG_RE = re.compile(r"package=([\w.]+)")
STATE_NAMES = {0: "none", 1: "stopped", 2: "paused", 3: "playing", 6: "buffering", 7: "error"}

_lock = threading.Lock()
_last = {"sample": None, "at": 0.0}


def _indent(line):
    return len(line) - len(line.lstrip())


def sap_block(dump, pkg=SAP_PKG):
    lines = dump.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == f"package={pkg}":
            ind = _indent(line)
            start = i
            while start > 0 and _indent(lines[start - 1]) >= ind:
                start -= 1
            end = i + 1
            while end < len(lines) and _indent(lines[end]) >= ind:
                end += 1
            return lines[max(0, start - 1):end]
    return None


def all_blocks(dump):
    """{package: block lines} for every media session in the dump (first session per package)."""
    out = {}
    for m in PKG_RE.finditer(dump):
        pkg = m.group(1)
        if pkg not in out:
            out[pkg] = sap_block(dump, pkg)
    return {k: v for k, v in out.items() if v}


def pick_player(dump, pkg=""):
    """Which app's session to follow. A set package wins. Otherwise Smart Audiobook Player if it
    has a session, else whichever app is playing right now, else one that is paused."""
    blocks = all_blocks(dump)
    if pkg:
        return pkg, blocks.get(pkg)
    if SAP_PKG in blocks:
        return SAP_PKG, blocks[SAP_PKG]
    best = None
    for p, b in blocks.items():
        m = STATE_RE.search("\n".join(b))
        if not m:
            continue
        st = int(m[1])
        if st == 3:
            return p, b
        if st == 2 and best is None:
            best = (p, b)
    return best if best else (None, None)


def adb_cmd(serial):
    return ["adb"] + (["-s", serial] if serial else [])


def sample(serial=None, pkg=""):
    if not shutil.which("adb"):
        return {"ok": False, "error": "adb is not installed"}
    try:
        res = subprocess.run(adb_cmd(serial) + ["shell", "cat /proc/uptime; dumpsys media_session"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=4)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "adb timed out (phone asleep or wireless link down?)"}
    if res.returncode != 0:
        err = (res.stderr or res.stdout).strip()
        if "more than one" in err:
            err = "More than one adb device. Pick one in the reader's phone settings."
        elif "no devices" in err or "not found" in err:
            err = "No phone connected over adb."
        elif "unauthorized" in err:
            err = "Phone is connected but adb is not authorized. Accept the prompt on the phone."
        return {"ok": False, "error": err[:200] or "adb failed"}

    first, _, dump = res.stdout.partition("\n")
    try:
        uptime_ms = float(first.split()[0]) * 1000
    except (ValueError, IndexError):
        return {"ok": False, "error": "Unexpected adb output"}
    player, block = pick_player(dump, pkg)
    if block is None:
        return {"ok": False, "connected": True,
                "error": "Phone connected, but no audio player is active. Open your book in the player and press play."}
    text = "\n".join(block)
    m = STATE_RE.search(text)
    if not m:
        return {"ok": False, "connected": True, "error": "The player has no playback state yet. Press play once."}
    state, pos, speed, updated = int(m[1]), int(m[2]), float(m[3]), int(m[4])
    age_ms = uptime_ms - updated
    playing = state == 3
    if speed <= 0:
        speed = 1.0
    live = pos + (age_ms * speed if playing else 0)
    meta = next((l.strip()[len("metadata:"):].strip() for l in block if l.strip().startswith("metadata:")), "")
    meta = re.sub(r"^size=\d+,\s*description=", "", meta)
    return {"ok": True, "connected": True, "player": player, "state": STATE_NAMES.get(state, str(state)), "playing": playing,
            "position": round(live / 1000, 3), "reported": pos / 1000, "speed": speed,
            "age": round(age_ms / 1000, 1), "meta": meta[:160], "sampled_at": time.time()}


def get(serial=None, max_age=0.7, pkg=""):
    """Cached sample, so several open tabs don't run adb several times a second."""
    with _lock:
        s, at = _last["sample"], _last["at"]
        if s is not None and time.time() - at < max_age:
            out = dict(s)
            if out.get("ok") and out["playing"]:
                out["position"] = round(out["position"] + (time.time() - at) * out["speed"], 3)
            return out
        s = sample(serial, pkg)
        _last["sample"], _last["at"] = s, time.time()
        return dict(s)


def devices():
    if not shutil.which("adb"):
        return []
    res = subprocess.run(["adb", "devices", "-l"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
    out = []
    for line in res.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            model = next((p.split(":", 1)[1] for p in parts[2:] if p.startswith("model:")), "")
            out.append({"serial": parts[0], "state": parts[1], "model": model})
    return out


def connect(address):
    if not shutil.which("adb"):
        return {"ok": False, "message": "adb is not installed"}
    address = address.strip()
    if not re.fullmatch(r"[\w.\-:\[\]]+", address):
        return {"ok": False, "message": "Address looks wrong. Use host:port, e.g. 192.168.1.23:5555"}
    res = subprocess.run(["adb", "connect", address], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
    msg = (res.stdout + res.stderr).strip()
    return {"ok": "connected" in msg and "cannot" not in msg and "failed" not in msg, "message": msg[:200]}


# ---------------- phone files (import) ----------------

AUDIO_EXT = (".m4b", ".m4a")
EPUB_EXT = (".epub",)


def _shq(s):
    """Quote a string for the phone's sh."""
    return "'" + s.replace("'", "'\\''") + "'"


def scan(serial=None, folders=None, timeout=60):
    """List audiobooks and EPUBs on the phone. folders: list of phone paths; empty = whole storage."""
    if not shutil.which("adb"):
        return {"ok": False, "error": "adb is not installed"}
    roots = [f.rstrip("/") + "/" for f in (folders or []) if f.strip()] or ["/sdcard/"]
    cmd = ("find " + " ".join(_shq(r) for r in roots) +
           " \\( -path '*/Android/*' -o -path '*/.*' \\) -prune -o -type f"
           " \\( -iname '*.m4b' -o -iname '*.m4a' -o -iname '*.epub' \\)"
           " -exec stat -c '%s|%n' {} + 2>/dev/null")
    try:
        res = subprocess.run(adb_cmd(serial) + ["shell", cmd], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Scanning the phone took too long. Set folders to search in."}
    err = (res.stderr or "").strip()
    if res.returncode != 0 and not res.stdout.strip():
        low = err.lower()
        if "no devices" in low or "not found" in low and "device" in low:
            return {"ok": False, "error": "No phone connected. Plug it in with USB debugging on (see README)."}
        if "unauthorized" in low:
            return {"ok": False, "error": "The phone is connected but not allowed yet. Tap Allow on the phone."}
        if "more than one" in low:
            return {"ok": False, "error": "More than one phone or device is connected. Unplug the extra one."}
        if "permission denied" not in low:   # find exits 1 on unreadable folders; that's fine
            return {"ok": False, "error": (err or "adb failed")[:200]}
    files = []
    for line in res.stdout.splitlines():
        size, _, path = line.partition("|")
        if not path or not size.strip().isdigit():
            continue
        low = path.lower()
        kind = "audio" if low.endswith(AUDIO_EXT) else "epub" if low.endswith(EPUB_EXT) else None
        if kind:
            name = path.rsplit("/", 1)[-1]
            folder = path.rsplit("/", 2)[-2] if path.count("/") >= 2 else ""
            files.append({"path": path, "size": int(size), "kind": kind, "name": name, "folder": folder})
    files.sort(key=lambda f: f["path"].lower())
    return {"ok": True, "files": files}


def pull(serial, src, dst, size=None, on_progress=None):
    """Copy a file from the phone. on_progress(bytes_done, total) is called about every half second."""
    import os
    proc = subprocess.Popen(adb_cmd(serial) + ["pull", src, str(dst)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    while proc.poll() is None:
        if on_progress:
            try:
                on_progress(os.path.getsize(dst), size)
            except OSError:
                on_progress(0, size)
        time.sleep(0.5)
    out, err = proc.communicate()
    if proc.returncode != 0:
        return False, (err or out).strip()[:300] or "adb pull failed"
    if on_progress and size:
        on_progress(size, size)
    return True, ""


def file_size(serial, path):
    try:
        res = subprocess.run(adb_cmd(serial) + ["shell", "stat -c %s " + _shq(path)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        return int(res.stdout.strip())
    except (ValueError, subprocess.TimeoutExpired):
        return None
