#!/usr/bin/env python3
"""
Behaviour tests for the sign's answer to the backend's alert channel check (security register I4):
the backend pings every sign on pat-sig/{deviceId}/status and the sign answers on /action. Run from
the repo root:

    uv run python tests/test_alert_channel_ping.py

Uses the real handlers.handle_status_message with the `home` app stubbed: no database, no broker,
no Django project. Portable (Windows and POSIX).
"""
import importlib.util
import json
import os
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
HANDLERS = os.path.join(HERE, "..", "src", "pat_sig", "module", "dsm", "home", "handlers.py")
DEVICE = "TEST-DEV"
FAILS = []

PUBLISHED = []  # (topic, payload, qos) sent directly
QUEUED = []  # (topic, payload) sent through the outbox
TASK_LOOKUPS = []


def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        FAILS.append(name)


class _DoesNotExist(Exception):
    pass


class _Tasks:
    @staticmethod
    def get(**kw):
        TASK_LOOKUPS.append(kw)
        raise _DoesNotExist()


def load_handlers():
    from django.conf import settings

    settings.configure(DEVICE_ID=DEVICE)
    home = types.ModuleType("home")
    home.__path__ = []
    services = types.ModuleType("home.services")
    services.__path__ = []
    models = types.ModuleType("home.models")
    models.DSMTask = types.SimpleNamespace(objects=_Tasks, DoesNotExist=_DoesNotExist)
    fake = types.ModuleType("home.services.mqtt_service")
    fake.mqtt_service = types.SimpleNamespace(
        publish=lambda topic, payload, qos=0: PUBLISHED.append((topic, payload, qos)) or True,
        publish_reliable=lambda topic, payload: QUEUED.append((topic, payload)),
    )
    sys.modules.update(
        {"home": home, "home.services": services, "home.models": models, "home.services.mqtt_service": fake}
    )
    spec = importlib.util.spec_from_file_location("home.handlers", HANDLERS)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["home.handlers"] = mod
    spec.loader.exec_module(mod)
    mod.logger.disabled = True
    return mod, settings


def main():
    handlers, settings = load_handlers()

    before = int(time.time() * 1000)
    handlers.handle_status_message(json.dumps({"type": "ping", "nonce": "n-1", "sentAt": 1234}))
    after = int(time.time() * 1000)
    check("a ping is answered once, on the sign's /action topic", [p[0] for p in PUBLISHED] == [f"pat-sig/{DEVICE}/action"])
    body = json.loads(PUBLISHED[0][1]) if PUBLISHED else {}
    check("... as a pong echoing the nonce and sentAt", body.get("type") == "pong" and body.get("nonce") == "n-1" and body.get("sentAt") == 1234)
    check("... stamped with the sign's time", isinstance(body.get("at"), int) and before <= body["at"] <= after)
    check("... at QoS 1", PUBLISHED and PUBLISHED[0][2] == 1)
    check("... directly, never through the outbox", QUEUED == [])
    check("a ping touches no task", TASK_LOOKUPS == [])

    PUBLISHED.clear()
    handlers.handle_status_message(json.dumps({"DSMTaskId": "task-1", "status": "STOP"}))
    check("a STOP command still goes to the task path", TASK_LOOKUPS == [{"dsm_task_id": "task-1"}])
    check("... and is not answered as a ping", PUBLISHED == [])

    settings.DEVICE_ID = ""
    handlers.handle_status_message(json.dumps({"type": "ping", "nonce": "n-2"}))
    check("a sign without a DEVICE_ID sends nothing", PUBLISHED == [])

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED")
        sys.exit(1)
    print("all passed")


if __name__ == "__main__":
    main()
