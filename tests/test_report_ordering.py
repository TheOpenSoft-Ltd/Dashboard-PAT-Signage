#!/usr/bin/env python3
"""
Behaviour tests for how the sign reports task status to the backend (security register I5):
every report carries the sign's own time, and the outbox sends each queued report once, in order,
however many threads flush it at the same moment. Run from the repo root:

    uv run python tests/test_report_ordering.py

Uses the real report builders (handlers._report_status, SchedulerService._report_action) and the
real mqtt_service against the fake broker from test_mqtt_reconnect.py, with the `home` app
stubbed: no database, no broker, no Django project. Portable (Windows and POSIX).
"""
import importlib.util
import json
import os
import sys
import threading
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.join(HERE, "..", "src", "pat_sig", "module", "dsm", "home")
sys.path.insert(0, HERE)
from test_mqtt_reconnect import FakeBroker, wait_for  # noqa: E402

DEVICE = "TEST-DEV"
FAILS = []


def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        FAILS.append(name)


# --------------------------------------------------------------------------- stubs
class Row:
    def __init__(self, pk, payload):
        self.pk, self.topic, self.payload, self.attempts = pk, f"pat-sig/{DEVICE}/action", payload, 0

    def delete(self):
        if self.pk == Outbox.fail_delete_pk:
            raise RuntimeError("database is locked")
        with Outbox.lock:
            if self in Outbox.rows:  # deleting a deleted row is a no-op, as in Django
                Outbox.rows.remove(self)

    def save(self, update_fields=None):
        pass


class Outbox:
    """Stands in for home.models.OutboxReport."""

    rows = []
    lock = threading.Lock()
    fail_delete_pk = None

    class objects:
        @staticmethod
        def all():
            class _Q:
                def order_by(self, *a):
                    with Outbox.lock:
                        snapshot = list(Outbox.rows)
                    time.sleep(0.05)  # widen the window two unguarded flushes would share
                    return snapshot

            return _Q()


SENT = []  # (topic, payload) the builders handed to publish_reliable


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def setup(broker):
    from django.conf import settings

    settings.configure(
        DEVICE_ID=DEVICE,
        MQTT_BROKER="127.0.0.1",
        MQTT_PORT=broker.port,
        MQTT_KEEPALIVE=5,
        USE_TZ=True,
        TIME_ZONE="Asia/Bangkok",
    )
    home = types.ModuleType("home")
    home.__path__ = []
    services = types.ModuleType("home.services")
    services.__path__ = []
    models = types.ModuleType("home.models")
    models.OutboxReport = Outbox
    models.DSMTask = object
    models.TaskType = types.SimpleNamespace(PUBLICRELATION="PUBLICRELATION")
    signals = types.ModuleType("home.signals")
    signals.mqtt_message_received = types.SimpleNamespace(send=lambda **kw: None)
    # The builders publish through this stand-in; the real service is loaded separately below.
    fake_service = types.ModuleType("home.services.mqtt_service")
    fake_service.mqtt_service = types.SimpleNamespace(
        publish_reliable=lambda topic, payload: SENT.append((topic, payload)),
        flush_outbox=lambda: None,
    )
    sys.modules.update(
        {
            "home": home,
            "home.services": services,
            "home.models": models,
            "home.signals": signals,
            "home.services.mqtt_service": fake_service,
        }
    )
    handlers = load("home.handlers", os.path.join(HOME, "handlers.py"))
    scheduler = load("home.services.scheduler_service", os.path.join(HOME, "services", "scheduler_service.py"))
    real = load("mqtt_service_under_test", os.path.join(HOME, "services", "mqtt_service.py"))
    real.logger.disabled = True
    return handlers, scheduler, real.mqtt_service


def sent_by(fn):
    SENT.clear()
    before = int(time.time() * 1000)
    fn()
    after = int(time.time() * 1000)
    body = json.loads(SENT[-1][1])
    return body, before, after


def main():
    broker = FakeBroker()
    broker.start()
    handlers, scheduler, svc = setup(broker)
    task = types.SimpleNamespace(dsm_task_id="task-1", dsm_id="dsm-1", name="clip")

    # 1. Every report carries the sign's time.
    body, before, after = sent_by(lambda: handlers._report_status(task, "playing"))
    check(
        "alert path (handlers._report_status) stamps `at`, epoch ms",
        isinstance(body.get("at"), int) and before <= body["at"] <= after,
    )
    body, before, after = sent_by(
        lambda: scheduler.SchedulerService._report_action(task, "completed", "2026-09-30")
    )
    check(
        "scheduled path (SchedulerService._report_action) stamps `at`, epoch ms",
        isinstance(body.get("at"), int) and before <= body["at"] <= after,
    )
    check("... and still sends `date` for a finished round", body.get("date") == "2026-09-30")

    # 2. Four flushes at once (paho's thread on connect, the scheduler tick, two new reports):
    #    every queued report goes out once, in order.
    svc._client.reconnect_delay_set(min_delay=1, max_delay=1)
    svc.connect()
    check("client connected to the fake broker", wait_for(svc.is_connected))
    wait_for(lambda: len(broker.subscribed) >= 3)
    Outbox.rows[:] = [Row(i, f"r{i:02d}".encode()) for i in range(30)]
    broker.published.clear()
    threads = [threading.Thread(target=svc.flush_outbox) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    wait_for(lambda: len(broker.published) >= 30, timeout=5)
    time.sleep(0.5)
    got = [p for _, p in broker.published]
    want = [f"r{i:02d}".encode() for i in range(30)]
    check("concurrent flushes: each report sent exactly once", sorted(got) == want)
    check("... in the order it was queued", got == want)
    check("... and the outbox is empty", not Outbox.rows)

    # 3. A locked database mid-flush: flush_outbox does not raise, stops in order, and the next
    #    flush carries on (the report that was sent but not deleted goes again; the backend
    #    drops the repeat by its `at`).
    Outbox.rows[:] = [Row(i, f"s{i}".encode()) for i in range(5)]
    Outbox.fail_delete_pk = 1
    broker.published.clear()
    try:
        svc.flush_outbox()
        raised = False
    except Exception:
        raised = True
    time.sleep(0.5)
    check("locked database: flush_outbox does not raise", not raised)
    check("... stops at the row it could not delete", [p for _, p in broker.published] == [b"s0", b"s1"])
    Outbox.fail_delete_pk = None
    broker.published.clear()
    svc.flush_outbox()
    wait_for(lambda: len(broker.published) >= 4, timeout=5)
    check("... and the next flush sends the rest, in order", [p for _, p in broker.published] == [b"s1", b"s2", b"s3", b"s4"])

    svc.disconnect()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED")
        sys.exit(1)
    print("all passed")


if __name__ == "__main__":
    main()
