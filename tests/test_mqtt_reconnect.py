#!/usr/bin/env python3
"""
Behaviour tests for the sign's MQTT client, home/services/mqtt_service.py (security register I2),
against the real paho-mqtt from uv.lock and a fake broker on localhost. Run from the repo root:

    uv run python tests/test_mqtt_reconnect.py

The fake broker speaks just enough MQTT 3.1.1 for one client (CONNACK, SUBACK, PUBACK, PINGRESP),
can be stopped and restarted on the same port, can drop the open connection, and can publish to the
client. Django settings are configured in-process and the `home` app is stubbed, so no database,
no broker and no Django project are needed. Portable (Windows and POSIX).
"""
import importlib.util
import logging
import os
import socket
import sys
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE = os.path.join(
    HERE, "..", "src", "pat_sig", "module", "dsm", "home", "services", "mqtt_service.py"
)
DEVICE = "TEST-DEV"


# --------------------------------------------------------------------------- fake broker
def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def _read_len(sock):
    mult, value = 1, 0
    while True:
        byte = _recv_exact(sock, 1)[0]
        value += (byte & 0x7F) * mult
        if not byte & 0x80:
            return value
        mult *= 128


def _enc_len(n):
    out = b""
    while True:
        byte, n = n % 128, n // 128
        out += bytes([byte | (0x80 if n else 0)])
        if not n:
            return out


class FakeBroker:
    def __init__(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.port = probe.getsockname()[1]
        probe.close()
        self.connects = 0
        self.subscribed = []  # topics, in the order the client subscribed
        self.published = []  # (topic, payload) the client sent
        self._srv = None
        self._conns = []
        self._lock = threading.Lock()

    def start(self):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.port))
        srv.listen(5)
        self._srv = srv
        threading.Thread(target=self._accept, args=(srv,), daemon=True).start()

    def stop(self):
        """Broker down: refuse new connections and drop the open one."""
        if self._srv:
            self._srv.close()
            self._srv = None
        self.kick()

    def kick(self):
        """Drop the open connections (a network blip, a broker pod killed)."""
        with self._lock:
            conns, self._conns = self._conns, []
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.close()

    def publish_to_client(self, topic, payload):
        t = topic.encode()
        body = len(t).to_bytes(2, "big") + t + payload
        pkt = bytes([0x30]) + _enc_len(len(body)) + body
        with self._lock:
            for c in self._conns:
                c.sendall(pkt)

    def _accept(self, srv):
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            with self._lock:
                self._conns.append(c)
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        try:
            while True:
                head = _recv_exact(c, 1)[0]
                n = _read_len(c)
                body = _recv_exact(c, n) if n else b""
                kind = head & 0xF0
                if kind == 0x10:  # CONNECT
                    self.connects += 1
                    c.sendall(b"\x20\x02\x00\x00")
                elif kind == 0x80:  # SUBSCRIBE
                    pid, i, codes = body[:2], 2, b""
                    while i < len(body):
                        ln = int.from_bytes(body[i : i + 2], "big")
                        self.subscribed.append(body[i + 2 : i + 2 + ln].decode())
                        i += 2 + ln + 1
                        codes += b"\x00"
                    c.sendall(bytes([0x90, 2 + len(codes)]) + pid + codes)
                elif kind == 0x30:  # PUBLISH from the client
                    ln = int.from_bytes(body[:2], "big")
                    topic, rest = body[2 : 2 + ln].decode(), body[2 + ln :]
                    if (head >> 1) & 3:
                        c.sendall(b"\x40\x02" + rest[:2])
                        rest = rest[2:]
                    self.published.append((topic, rest))
                elif kind == 0xC0:  # PINGREQ
                    c.sendall(b"\xd0\x00")
                elif kind == 0xE0:  # DISCONNECT
                    return
        except (OSError, ConnectionError):
            return
        finally:
            with self._lock:
                if c in self._conns:
                    self._conns.remove(c)
            c.close()


# --------------------------------------------------------------------------- the service
class _Records(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())

    def count(self, text):
        return sum(text in line for line in self.lines)


class _Outbox:
    """Stands in for home.models.OutboxReport; empty unless a test fills it."""

    rows = []

    class objects:
        @staticmethod
        def all():
            class _Q:
                def order_by(self, *a):
                    return list(_Outbox.rows)

            return _Q()

        @staticmethod
        def create(topic, payload):
            _Outbox.rows.append(types.SimpleNamespace(topic=topic, payload=payload))


def load_service(broker):
    from django.conf import settings

    settings.configure(
        DEVICE_ID=DEVICE, MQTT_BROKER="127.0.0.1", MQTT_PORT=broker.port, MQTT_KEEPALIVE=5
    )
    home = types.ModuleType("home")
    home.__path__ = []
    models = types.ModuleType("home.models")
    models.OutboxReport = _Outbox
    signals = types.ModuleType("home.signals")
    signals.mqtt_message_received = types.SimpleNamespace(send=lambda **kw: None)
    sys.modules.update({"home": home, "home.models": models, "home.signals": signals})

    spec = importlib.util.spec_from_file_location("mqtt_service_under_test", SERVICE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.SUPERVISE_S = 1  # check the network thread every second, not every 30
    svc = mod.mqtt_service
    svc._client.reconnect_delay_set(min_delay=1, max_delay=1)  # 1 s retries, not 5
    records = _Records()
    mod.logger.addHandler(records)
    mod.logger.setLevel(logging.INFO)
    return mod, svc, records


def wait_for(cond, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


# --------------------------------------------------------------------------- tests
FAILS = []


def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        FAILS.append(name)


def main():
    broker = FakeBroker()
    mod, svc, log = load_service(broker)
    topics = [f"pat-sig/{DEVICE}/data", f"pat-sig/{DEVICE}/status", f"pat-sig/{DEVICE}/alert"]

    # 1. Broker down at boot: connect() must not raise, and paho keeps trying.
    svc.connect()
    time.sleep(2.5)
    check("broker down at boot: not connected, connect() did not raise", not svc.is_connected())
    check(
        "broker down at boot: the watchdog's 'reconnecting' line is logged",
        log.count("MQTT reconnecting in 5 seconds") >= 1,
    )
    broker.start()
    check("broker comes up: connects by itself", wait_for(svc.is_connected))
    check("... and subscribes to data/status/alert", wait_for(lambda: broker.subscribed[:3] == topics))
    check("... logs 'MQTT connected successfully'", log.count("MQTT connected successfully") == 1)
    check(
        "... logs the exact 'MQTT subscribed to:' line the deploy scripts grep",
        log.count(f"MQTT subscribed to: {', '.join(topics)}") == 1,
    )

    # 2. The connection drops: paho reconnects and subscribes again.
    subs = len(broker.subscribed)
    broker.kick()
    check("connection dropped: reconnects", wait_for(lambda: broker.connects >= 2 and svc.is_connected()))
    check("... and re-subscribes", wait_for(lambda: len(broker.subscribed) >= subs + 3))

    # 3. Broker restart (power cut, pod rescheduled).
    broker.stop()
    check("broker stopped: is_connected() goes False", wait_for(lambda: not svc.is_connected()))
    time.sleep(2)
    connects = broker.connects
    broker.start()
    check("broker back: reconnects", wait_for(lambda: broker.connects > connects and svc.is_connected()))

    # 3b. The I2 race: nothing may tear down the fresh connection afterwards. The old service ran
    #     its own reconnect thread beside paho's; it woke 5 s later, did loop_stop() + reconnect()
    #     on the connection paho had just made, and when that reconnect failed the sign was left
    #     with no network thread at all.
    connects = broker.connects
    time.sleep(7)
    check(
        "after a broker restart nothing tears the fresh connection down",
        broker.connects == connects and svc.is_connected(),
    )

    # 4. The I2 failure: paho's network thread dies while the client still reports connected. The
    #    supervisor must notice (is_connected() cannot) and bring the loop back.
    thread = svc._client._thread
    svc._client._thread_terminate = True  # the thread leaves loop_forever and ends
    thread.join(5)
    check("network thread killed while paho still says connected", not thread.is_alive())
    check(
        "supervisor restarts the network thread",
        wait_for(lambda: svc._client._thread is not None and svc._client._thread.is_alive()),
    )
    check("... and says so in the log", log.count("MQTT network thread is not running") >= 1)
    connects = broker.connects
    broker.kick()
    check(
        "the restarted loop still reconnects after a drop",
        wait_for(lambda: broker.connects > connects and svc.is_connected()),
    )

    # 5. A callback that raises must not end the network thread (in paho a raising callback ends
    #    it, which is what the old service's on_connect -> flush_outbox could do on a locked db).
    original = svc._client.on_message
    svc._client.on_message = lambda *a: 1 / 0
    thread = svc._client._thread
    broker.publish_to_client(f"pat-sig/{DEVICE}/alert", b"{}")
    time.sleep(0.5)  # well inside SUPERVISE_S: the same thread, not a restarted one
    check(
        "a raising callback leaves the network thread running",
        thread is not None and svc._client._thread is thread and thread.is_alive(),
    )
    check("... and the client connected", svc.is_connected())
    svc._client.on_message = original

    # 6. disconnect() stops for good: the supervisor does not bring the loop back.
    svc.disconnect()
    time.sleep(2.5)
    check("disconnect(): the loop stays stopped", svc._client._thread is None)

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED")
        sys.exit(1)
    print("all passed")


if __name__ == "__main__":
    main()
