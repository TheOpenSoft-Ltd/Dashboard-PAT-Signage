#!/usr/bin/env python3
"""
stream_supervisor.py - the sign's screen stream (X11 capture -> RTMP) with a progress watchdog.

Runs as the pat-sig-stream ExecStart on X11 signs in place of stream.sh. It launches the SAME ffmpeg
command as stream.sh's X11 path, adds `-progress pipe:1`, reads ffmpeg's own output clock
(`out_time_us`, reported about twice a second) and SIGKILLs ffmpeg when that clock stops:

  STREAM_STALL_TTL  seconds without progress after streaming started   (default 20)
  STREAM_START_TTL  seconds allowed before the first progress line     (default 45; RTMP handshake over 4G)

Why: after a 4G blip the carrier drops the NAT mapping and the RTMP socket becomes a black hole.
ffmpeg 4.3.9 (Debian 11) has no output-side socket timeout, so it blocks in write() forever with its
encoder threads still busy; it also ignores SIGTERM, so systemd's stop waits out its timeout. On
2026-09-24/25 the healer's bytes_acked check caught 8 such wedges on 5 signs, each ~8 min dark
(~6.5 min to be sure from outside the process + ~1.5 min to kill). Reading ffmpeg's own progress,
a wedge is unambiguous within seconds and the kill is immediate: ~STALL_TTL + a few seconds dark.
The healer's socket check stays in place as the independent backstop.

Kept from stream.sh: config keys and precedence (unit environment > argv[1] for RTMP_URL > the .env
file, whitelisted keys only), the probe of the ingest before launching the encoder, and backoff across
failed attempts (a run shorter than STREAM_BACKOFF_FAST s counts as failed; waits double from 10 s,
capped at STREAM_BACKOFF_CAP). On a Wayland session it hands over to stream.sh (wf-recorder path,
no watchdog) - every sign today is X11.

ffmpeg 4.3.9 has no `-stats_period` (the station supervisor passes it); progress is emitted at the
default stats interval, which is frequent enough. Standard library only; Python >= 3.7.

  python3 stream_supervisor.py                 # supervise (systemd)
  python3 stream_supervisor.py --print-cmd     # print the ffmpeg command with the URL redacted, exit
"""
import os
import random
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

ENV_KEYS = (
    "RTMP_URL", "STREAM_FPS", "STREAM_RESOLUTION", "STREAM_BITRATE", "STREAM_ENCODER", "STREAM_AUDIO",
    "STREAM_BACKOFF_FAST", "STREAM_BACKOFF_CAP", "STREAM_STALL_TTL", "STREAM_START_TTL",
)
STREAM_SH = os.environ.get("PAT_SIG_STREAM_SH", "/usr/local/bin/pat-sig-stream.sh")


def log(msg):
    print("[pat-sig][stream] %s" % msg, flush=True)


def load_config(argv):
    """Unit environment wins, then argv[1] (RTMP_URL only), then the .env file (whitelisted keys)."""
    cfg = {}
    env_file = os.environ.get("PAT_SIG_ENV") or os.path.expanduser("~/.config/pat-sig/dsm/.env")
    try:
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k in ENV_KEYS:
                    cfg[k] = v
    except OSError:
        pass
    for k in ENV_KEYS:
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    if len(argv) > 1 and argv[1] and not argv[1].startswith("--"):
        cfg["RTMP_URL"] = argv[1]
    return cfg


def fnum(cfg, key, default):
    try:
        return float(cfg.get(key) or default)
    except ValueError:
        return float(default)


def build_cmd(cfg, display):
    """stream.sh's X11 command, token for token, plus -nostdin and -progress pipe:1."""
    fps = int(cfg.get("STREAM_FPS") or 25)
    bitrate = cfg.get("STREAM_BITRATE") or "2500k"
    encoder = cfg.get("STREAM_ENCODER") or "libx264"
    audio = cfg.get("STREAM_AUDIO") or "silent"
    resolution = cfg.get("STREAM_RESOLUTION") or ""

    enc = ["-c:v", encoder]
    if encoder == "libx264":
        enc += ["-preset", "ultrafast", "-tune", "zerolatency"]
    enc += ["-pix_fmt", "yuv420p", "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", bitrate, "-g", str(fps * 2)]
    scale = ["-vf", "scale=" + resolution.replace("x", ":", 1)] if resolution else []
    if audio == "silent":
        audio_in = ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        audio_args = ["-c:a", "aac", "-b:a", "128k", "-map", "0:v:0", "-map", "1:a:0"]
    elif audio == "pulse":
        audio_in = ["-f", "pulse", "-i", "default"]
        audio_args = ["-c:a", "aac", "-b:a", "128k", "-map", "0:v:0", "-map", "1:a:0"]
    elif audio == "none":
        audio_in, audio_args = [], ["-an"]
    else:
        raise SystemExit("STREAM_AUDIO must be silent|pulse|none (got %r)" % audio)

    ffmpeg = shlex.split(os.environ.get("STREAM_FFMPEG") or "ffmpeg")   # override for tests only
    return (ffmpeg + ["-nostdin", "-hide_banner", "-loglevel", "warning", "-progress", "pipe:1",
                      "-f", "x11grab", "-framerate", str(fps), "-i", display]
            + audio_in + scale + enc + audio_args + ["-f", "flv", cfg["RTMP_URL"]])


def ingest_reachable(url, timeout=5):
    p = urlparse(url)
    try:
        with socket.create_connection((p.hostname, p.port or 1935), timeout=timeout):
            return True
    except Exception:
        return False


class Run:
    """One ffmpeg process and what it reported."""

    def __init__(self, cmd):
        self.cmd = cmd
        self.started = time.monotonic()
        self.last_advance = self.started
        self.out_us = -1
        self.frames = 0
        self.total_size = 0
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, universal_newlines=True, bufsize=1)
        threading.Thread(target=self._read, daemon=True).start()

    @property
    def progressed(self):
        return self.out_us >= 0

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if line.startswith("out_time_us="):
                try:
                    v = int(line.split("=", 1)[1])
                except ValueError:
                    continue                      # "N/A" before the first packet
                if v > self.out_us:
                    self.out_us = v
                    self.last_advance = time.monotonic()
            elif line.startswith("frame="):
                try:
                    self.frames = int(line.split("=", 1)[1])
                except ValueError:
                    pass
            elif line.startswith("total_size="):
                try:
                    self.total_size = int(line.split("=", 1)[1])
                except ValueError:
                    pass
            elif line and "=" not in line.split(" ", 1)[0]:
                log("[ffmpeg] " + line)           # a log message, not a progress key=value

    def kill(self):
        if self.proc.poll() is None:
            try:
                self.proc.kill()                  # SIGKILL: a wedged ffmpeg ignores SIGTERM
            except ProcessLookupError:
                pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


CURRENT = {"run": None}


def on_term(signum, _frame):
    """systemd stop/restart: take ffmpeg down at once instead of waiting out the stop timeout."""
    run = CURRENT["run"]
    if run is not None:
        run.kill()
    log("stopping on signal %d" % signum)
    sys.exit(0)


def supervise(cfg):
    stall_ttl = fnum(cfg, "STREAM_STALL_TTL", 20)
    start_ttl = fnum(cfg, "STREAM_START_TTL", 45)
    fast = fnum(cfg, "STREAM_BACKOFF_FAST", 60)
    cap = fnum(cfg, "STREAM_BACKOFF_CAP", 100)
    display = os.environ.get("DISPLAY") or ":0"
    cmd = build_cmd(cfg, display)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    log("supervisor up: X11 %s -> ingest, stall_ttl=%.0fs start_ttl=%.0fs backoff fast<%.0fs cap=%.0fs"
        % (display, stall_ttl, start_ttl, fast, cap))

    failures = 0
    while True:
        if not ingest_reachable(cfg["RTMP_URL"]):
            failures += 1
            delay = min(10 * 2 ** (failures - 1), cap)
            log("ingest unreachable - not starting the encoder (attempt %d), retry in %.0fs" % (failures, delay))
            time.sleep(delay)
            continue

        run = Run(cmd)
        CURRENT["run"] = run
        announced = False
        reason = "exit"
        while True:
            if run.proc.poll() is not None:
                reason = "exit rc=%s" % run.proc.returncode
                break
            now = time.monotonic()
            if run.progressed:
                if not announced:
                    announced = True
                    log("streaming (first progress %.1fs after launch)" % (run.last_advance - run.started))
                stalled = now - run.last_advance
                if stalled > stall_ttl:
                    reason = "frozen %.0fs (no progress)" % stalled
                    log("%s -> killing ffmpeg" % reason)
                    run.kill()
                    break
            elif now - run.started > start_ttl:
                reason = "no progress within %.0fs of launch" % start_ttl
                log("%s -> killing ffmpeg" % reason)
                run.kill()
                break
            time.sleep(1)
        CURRENT["run"] = None

        session = time.monotonic() - run.started
        if run.progressed and session >= fast:
            failures = 0
            delay = 2 + random.uniform(0, 1)      # a real session ended: come back fast
        else:
            failures += 1
            delay = min(10 * 2 ** (failures - 1), cap)
        log("ffmpeg ended: %s after %.0fs (progress=%s frames=%d bytes=%d) -> retry in %.0fs"
            % (reason, session, "yes" if run.progressed else "no", run.frames, run.total_size, delay))
        time.sleep(delay)


def main(argv):
    cfg = load_config(argv)
    if not cfg.get("RTMP_URL"):
        log("RTMP_URL not set (unit environment, argv, or the .env file)")
        return 1
    xdg = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
    wayland = os.environ.get("WAYLAND_DISPLAY") or (os.path.exists(os.path.join(xdg, "wayland-0")) and "wayland-0")
    if "--print-cmd" in argv:
        cmd = build_cmd(cfg, os.environ.get("DISPLAY") or ":0")
        print(" ".join("rtmp://<redacted>" if a == cfg["RTMP_URL"] else a for a in cmd))
        return 0
    if wayland:
        log("Wayland session - handing over to %s (wf-recorder path, no progress watchdog)" % STREAM_SH)
        os.execv("/bin/bash", ["bash", STREAM_SH] + argv[1:])
    supervise(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
