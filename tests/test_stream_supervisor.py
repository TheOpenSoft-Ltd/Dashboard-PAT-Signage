#!/usr/bin/env python3
"""
Behaviour tests for src/pat_sig/module/worker/stream_supervisor.py, run with plain python3 (no pytest):

    python3 tests/test_stream_supervisor.py

A fake ffmpeg (written to a temp dir) emits the same `-progress` key=value lines ffmpeg 4.3.9 does and
can stream, FREEZE while ignoring SIGTERM (what a wedged ffmpeg on a black-holed RTMP socket does),
never start, or exit. A local TCP listener stands in for the RTMP ingest. POSIX only (signals).
"""
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SUP = os.path.join(HERE, "..", "src", "pat_sig", "module", "worker", "stream_supervisor.py")

FAKE = r'''
import os, sys, time, signal
mode = os.environ.get("FAKE_MODE", "healthy")
with open(os.environ["FAKE_PIDFILE"], "a") as f:
    f.write("%d\n" % os.getpid())
signal.signal(signal.SIGTERM, signal.SIG_IGN)          # a wedged ffmpeg does not die on SIGTERM
us = 0
def tick():
    global us
    us += 500000
    sys.stdout.write("frame=%d\nfps=25.0\nout_time_us=%d\ntotal_size=%d\nprogress=continue\n" % (us // 40000, us, us // 3))
    sys.stdout.flush()
if mode == "nostart":
    sys.stdout.write("out_time_us=N/A\n"); sys.stdout.flush()
    while True: time.sleep(1)
if mode == "exit":
    for _ in range(2): tick(); time.sleep(0.5)
    sys.stderr.write("[flv @ 0x1] Failed to update header with correct duration.\n"); sys.stderr.flush()
    sys.exit(1)
if mode == "freeze":
    for _ in range(4): tick(); time.sleep(0.5)
    while True: time.sleep(1)                           # blocked in write(): no more progress
while True:
    tick(); time.sleep(0.5)
'''

LIVE_PISN002 = ("ffmpeg -hide_banner -loglevel warning -f x11grab -framerate 25 -i :0 -f lavfi -i "
                "anullsrc=channel_layout=stereo:sample_rate=44100 -c:v libx264 -preset ultrafast -tune zerolatency "
                "-pix_fmt yuv420p -b:v 2500k -maxrate 2500k -bufsize 2500k -g 50 -c:a aac -b:a 128k -map 0:v:0 "
                "-map 1:a:0 -f flv rtmp://<redacted>")   # /proc/<pid>/cmdline on PTY-PISN002, 2026-09-25

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print("%-66s %s%s" % (name, "PASS" if ok else "FAIL", "" if ok else "  <- " + detail), flush=True)


def listener():
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(16)
    def accept():
        while True:
            try:
                c, _ = s.accept()
                c.close()
            except OSError:
                return
    threading.Thread(target=accept, daemon=True).start()
    return s, s.getsockname()[1]


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Sup:
    def __init__(self, tmp, mode, port, extra_env=None, args=()):
        self.pidfile = os.path.join(tmp, "pids-%s-%d" % (mode, time.monotonic_ns()))
        open(self.pidfile, "w").close()
        fake = os.path.join(tmp, "fake_ffmpeg.py")
        env = dict(os.environ)
        env.update({
            "RTMP_URL": "rtmp://127.0.0.1:%d/DSMApp/STREAM-TEST" % port,
            "STREAM_FFMPEG": "%s %s" % (sys.executable, fake),
            "FAKE_MODE": mode, "FAKE_PIDFILE": self.pidfile,
            "STREAM_STALL_TTL": "3", "STREAM_START_TTL": "4",
            "STREAM_BACKOFF_FAST": "60", "STREAM_BACKOFF_CAP": "2",
            "PAT_SIG_ENV": os.path.join(tmp, "no.env"), "DISPLAY": ":99", "XDG_RUNTIME_DIR": tmp,
        })
        env.pop("WAYLAND_DISPLAY", None)
        env.update(extra_env or {})
        self.lines = []
        self.p = subprocess.Popen([sys.executable, SUP] + list(args), env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, universal_newlines=True, bufsize=1)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            self.lines.append((time.monotonic(), line.rstrip()))

    def wait_for(self, text, timeout, after=0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            hits = [t for t, l in self.lines[after:] if text in l]
            if hits:
                return hits[0]
            time.sleep(0.1)
        return None

    def index(self):
        return len(self.lines)

    def pids(self):
        with open(self.pidfile) as f:
            return [int(x) for x in f.read().split()]

    def stop(self):
        if self.p.poll() is None:
            self.p.send_signal(signal.SIGTERM)
            try:
                self.p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.p.kill()

    def text(self):
        return "\n".join(l for _, l in self.lines)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie is not alive for our purpose
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().split()[2] != "Z"
    except OSError:
        return True


def main():
    tmp = tempfile.mkdtemp(prefix="sup-test-")
    with open(os.path.join(tmp, "fake_ffmpeg.py"), "w") as f:
        f.write(FAKE)
    srv, port = listener()

    # T1 healthy stream: streaming announced, never killed
    s = Sup(tmp, "healthy", port)
    t0 = time.monotonic()
    ok = s.wait_for("streaming", 5)
    time.sleep(6)
    check("T1 healthy: 'streaming' announced", ok is not None, s.text()[-300:])
    check("T1 healthy: no kill over 6 s with stall_ttl 3 s", "killing" not in s.text(), s.text()[-300:])
    check("T1 healthy: exactly one ffmpeg launched", len(s.pids()) == 1, str(s.pids()))
    s.stop()

    # T2 wedge: progress stops and ffmpeg ignores SIGTERM -> SIGKILLed after ~stall_ttl, relaunched
    s = Sup(tmp, "freeze", port)
    t_stream = s.wait_for("streaming", 6)
    t_kill = s.wait_for("frozen", 10)
    check("T2 freeze: detected as frozen", t_kill is not None, s.text()[-400:])
    if t_stream and t_kill:
        # fake stops ticking ~2 s after 'streaming'; kill must follow within stall_ttl(3) + ~2 s of that
        check("T2 freeze: killed within stall_ttl + 2 s of the last progress", t_kill - t_stream <= 2 + 3 + 2.5,
              "%.1fs" % (t_kill - t_stream))
    first = s.pids()[0] if s.pids() else None
    time.sleep(0.5)
    check("T2 freeze: the frozen ffmpeg is gone (SIGKILL, not SIGTERM)", first is not None and not alive(first),
          "pid %s alive" % first)
    t_second = s.wait_for("streaming", 8, after=[i for i, (t, l) in enumerate(s.lines) if "frozen" in l][0] + 1
                          if t_kill else 0)
    check("T2 freeze: relaunched and streaming again", t_second is not None and len(s.pids()) >= 2,
          "pids=%s" % s.pids())
    check("T2 freeze: end line carries reason and counters", "ffmpeg ended: frozen" in s.text() and "frames=" in s.text(),
          s.text()[-300:])
    s.stop()

    # T3 never produces progress (handshake hangs) -> killed after start_ttl, backs off
    s = Sup(tmp, "nostart", port)
    t_nostart = s.wait_for("no progress within", 8)
    check("T3 no-start: killed after start_ttl", t_nostart is not None, s.text()[-300:])
    check("T3 no-start: counted as a failure with backoff", s.wait_for("progress=no", 3) is not None, s.text()[-300:])
    first = s.pids()[0] if s.pids() else None
    time.sleep(0.5)
    check("T3 no-start: the hung ffmpeg is gone", first is not None and not alive(first), "pid %s" % first)
    s.stop()

    # T4 systemd stop while ffmpeg ignores SIGTERM: supervisor exits fast and takes ffmpeg down
    s = Sup(tmp, "healthy", port)
    s.wait_for("streaming", 5)
    pid = s.pids()[0]
    t = time.monotonic()
    s.p.send_signal(signal.SIGTERM)
    try:
        s.p.wait(timeout=6)
    except subprocess.TimeoutExpired:
        pass
    took = time.monotonic() - t
    check("T4 SIGTERM: supervisor exits within 3 s", s.p.poll() is not None and took < 3, "%.1fs rc=%s" % (took, s.p.poll()))
    time.sleep(0.3)
    check("T4 SIGTERM: ffmpeg (ignoring SIGTERM) is dead too", not alive(pid), "pid %d alive" % pid)
    s.stop()

    # T5 ingest unreachable: encoder never launched
    s = Sup(tmp, "healthy", closed_port())
    hit = s.wait_for("ingest unreachable", 8)
    check("T5 unreachable ingest: probe refuses, logged", hit is not None, s.text()[-300:])
    check("T5 unreachable ingest: no ffmpeg launched", s.pids() == [], str(s.pids()))
    s.stop()

    # T6 ffmpeg exits by itself: logged with its message and rc, relaunched after backoff
    s = Sup(tmp, "exit", port)
    hit = s.wait_for("ffmpeg ended: exit rc=1", 8)
    check("T6 ffmpeg exit: end logged with rc", hit is not None, s.text()[-300:])
    check("T6 ffmpeg exit: ffmpeg log line surfaced", "[ffmpeg] [flv @ 0x1]" in s.text(), s.text()[-300:])
    check("T6 ffmpeg exit: relaunched", s.wait_for("streaming", 8, after=s.index()) is not None or len(s.pids()) >= 2,
          str(s.pids()))
    s.stop()

    # T7 the command is stream.sh's, token for token, plus -nostdin and -progress pipe:1
    env = dict(os.environ, RTMP_URL="rtmp://rtmp.example/DSMApp/STREAM-X", DISPLAY=":0",
               PAT_SIG_ENV=os.path.join(tmp, "no.env"), XDG_RUNTIME_DIR=tmp)
    for k in ("STREAM_FFMPEG", "WAYLAND_DISPLAY", "STREAM_FPS", "STREAM_BITRATE", "STREAM_ENCODER", "STREAM_AUDIO",
              "STREAM_RESOLUTION"):
        env.pop(k, None)
    out = subprocess.run([sys.executable, SUP, "--print-cmd"], env=env, stdout=subprocess.PIPE,
                         universal_newlines=True).stdout.split()
    stripped = [a for a in out if a != "-nostdin"]
    i = stripped.index("-progress") if "-progress" in stripped else -1
    if i >= 0:
        del stripped[i:i + 2]
    check("T7 command == pisn002's live ffmpeg + -nostdin -progress pipe:1", " ".join(stripped) == LIVE_PISN002,
          "\n   got : %s\n   want: %s" % (" ".join(stripped), LIVE_PISN002))
    check("T7 no -stats_period (absent in ffmpeg 4.3.9)", "-stats_period" not in out, " ".join(out))

    # T8 config precedence: unit env > .env file; non-whitelisted keys ignored; RTMP_URL redacted in print
    envf = os.path.join(tmp, "test.env")
    with open(envf, "w") as f:
        f.write("STREAM_BITRATE=1500k\nRTMP_URL=rtmp://file.example/x/y\nEVIL=$(touch /tmp/pwned)\nSTREAM_FPS=15\n")
    env2 = dict(env, PAT_SIG_ENV=envf, STREAM_FPS="30")
    out = subprocess.run([sys.executable, SUP, "--print-cmd"], env=env2, stdout=subprocess.PIPE,
                         universal_newlines=True).stdout
    check("T8 .env value used when the unit does not set it (bitrate 1500k)", "-b:v 1500k" in out, out)
    check("T8 unit env wins over .env (fps 30, gop 60)", "-framerate 30" in out and "-g 60" in out, out)
    check("T8 URL never printed", "example" not in out and "rtmp://<redacted>" in out, out)

    srv.close()
    n, bad = len(RESULTS), RESULTS.count(False)
    print("TOTAL: %d/%d PASS" % (n - bad, n))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
