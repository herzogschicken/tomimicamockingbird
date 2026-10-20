#!/usr/bin/env python3
"""Does Smart Audiobook Player publish its playback position over adb?

Usage:
    python3 tools/adb_probe.py            # 5 samples, 2 s apart
    python3 tools/adb_probe.py -n 10 -i 1
    python3 tools/adb_probe.py --raw      # also print SAP's raw dumpsys block
    python3 tools/adb_probe.py -s <serial>  # pick a device if several are connected

It reads `adb shell dumpsys media_session`, finds SAP's session, and prints
state, position, speed and a live position estimate for each sample.
"""
import argparse
import re
import subprocess
import sys
import time

SAP_PKG = "ak.alizandro.smartaudiobookplayer"

STATE_NAMES = {0: "none", 1: "stopped", 2: "paused", 3: "playing", 4: "fast-forward",
               5: "rewind", 6: "buffering", 7: "error", 8: "connecting",
               9: "skip-prev", 10: "skip-next", 11: "skip-queue"}

STATE_RE = re.compile(
    r"state=PlaybackState \{state=(?:\w+\()?(\d+)\)?,\s*position=(-?\d+),"
    r".*?speed=([-\d.]+),\s*updated=(\d+)")
PKG_RE = re.compile(r"package=([\w.]+)")


def adb(args, serial=None):
    cmd = ["adb"] + (["-s", serial] if serial else []) + args
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    if res.returncode != 0:
        sys.exit(f"adb failed: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout


def sap_block(dump, pkg):
    """Lines belonging to the first session owned by pkg."""
    lines = dump.splitlines()
    for i, line in enumerate(lines):
        if f"package={pkg}" in line.strip():
            # walk back to the session header (less indented line)
            indent = len(line) - len(line.lstrip())
            start = i
            while start > 0 and (len(lines[start - 1]) - len(lines[start - 1].lstrip())) >= indent:
                start -= 1
            start = max(0, start - 1)
            end = i + 1
            while end < len(lines) and (len(lines[end]) - len(lines[end].lstrip())) >= indent:
                end += 1
            return lines[start:end]
    return None


def uptime_ms(serial):
    # /proc/uptime is CLOCK_BOOTTIME, the same clock as SystemClock.elapsedRealtime()
    return int(float(adb(["shell", "cat", "/proc/uptime"], serial).split()[0]) * 1000)


def fmt(ms):
    s = max(0, ms) // 1000
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}.{max(0, ms) % 1000:03d}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=5, help="number of samples")
    ap.add_argument("-i", type=float, default=2.0, help="seconds between samples")
    ap.add_argument("-s", "--serial", help="adb device serial")
    ap.add_argument("--pkg", default=SAP_PKG, help="player package name")
    ap.add_argument("--raw", action="store_true", help="print the raw session block")
    a = ap.parse_args()

    devices = [l for l in adb(["devices"]).splitlines()[1:] if l.strip().endswith("device")]
    if not devices:
        sys.exit("No adb device. Plug in USB (or `adb connect <phone-tailscale-ip>:5555`) and accept the prompt.")

    for n in range(a.n):
        dump = adb(["shell", "dumpsys", "media_session"], a.serial)
        block = sap_block(dump, a.pkg)
        if block is None:
            pkgs = sorted(set(PKG_RE.findall(dump)))
            print(f"No session for {a.pkg}. Packages with media sessions: {', '.join(pkgs) or 'none'}")
            print("Open SAP, start a book, then run again. If SAP uses another package name, pass --pkg.")
            return
        if a.raw and n == 0:
            print("----- raw SAP session -----")
            print("\n".join(block))
            print("---------------------------")
        text = "\n".join(block)
        m = STATE_RE.search(text)
        meta = next((l.strip() for l in block if l.strip().startswith("metadata:")), "metadata: (none)")
        if not m:
            print("SAP session found but no PlaybackState line. Run with --raw and send me the output.")
            return
        state, pos, speed, updated = int(m[1]), int(m[2]), float(m[3]), int(m[4])
        now = uptime_ms(a.serial)
        live = pos + int((now - updated) * speed) if state == 3 else pos
        print(f"[{n + 1}/{a.n}] state={STATE_NAMES.get(state, state)}  reported={fmt(pos)}  "
              f"speed={speed:g}  age={(now - updated) / 1000:.1f}s  live~{fmt(live)}")
        if n == 0:
            print(f"        {meta[:160]}")
        if n < a.n - 1:
            time.sleep(a.i)


if __name__ == "__main__":
    main()
