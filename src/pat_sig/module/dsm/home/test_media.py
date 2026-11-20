"""Large media on the sign: streaming download, resume, no double downloads,
and alerts keep priority over a PR clip.

Run: PAT_SIG_NO_SERVICES=1 python manage.py test home   (from module/dsm)
"""
import json
import re
import shutil
import tempfile
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from django.test import Client, TransactionTestCase, override_settings
from django.utils import timezone

from home import handlers
from home.media_download import fetch_to_file
from home.models import DSMTask
from home.task_downloads import TaskDownloads, task_downloads

BLOB = bytes(range(256)) * 4096  # 1 MiB, every byte position recognisable


class _Server:
    """An object store in miniature: serves BLOB with Range support, can cut a
    connection after N bytes (a 4G drop) and can be slowed down."""

    def __init__(self, blob=BLOB, content_type="video/mp4"):
        self.blob = blob
        self.content_type = content_type
        self.drop_after = None  # bytes sent before the FIRST response is cut
        self.delay = 0.0
        self.requests = []  # Range header of each request ("" if none)
        self.bytes_sent = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                rng = self.headers.get("Range", "")
                outer.requests.append(rng)
                if outer.delay:
                    time.sleep(outer.delay)
                data = outer.blob
                start, end = 0, len(data) - 1
                m = re.match(r"bytes=(\d+)-(\d*)", rng)
                if m:
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), end)
                    if start >= len(data):
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{len(data)}")
                        self.end_headers()
                        return
                    self.send_response(206)
                    self.send_header(
                        "Content-Range", f"bytes {start}-{end}/{len(data)}"
                    )
                else:
                    self.send_response(200)
                body = data[start : end + 1]
                self.send_header("Content-Type", outer.content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if outer.drop_after is not None:
                    cut, outer.drop_after = outer.drop_after, None
                    self.wfile.write(body[:cut])
                    outer.bytes_sent += cut
                    self.wfile.flush()
                    self.connection.shutdown(2)  # the 4G link goes away
                    return
                self.wfile.write(body)
                outer.bytes_sent += len(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/obj?sig=1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class _MediaTest(TransactionTestCase):
    def setUp(self):
        self.media = Path(tempfile.mkdtemp())
        self.settings_override = override_settings(MEDIA_ROOT=self.media)
        self.settings_override.enable()
        self.server = _Server()

    def tearDown(self):
        self.server.close()
        self.settings_override.disable()
        shutil.rmtree(self.media, ignore_errors=True)


class FetchToFileTests(_MediaTest):
    def test_streams_the_whole_object(self):
        part = self.media / "t.part"
        size, ctype = fetch_to_file(lambda: self.server.url, part)
        self.assertEqual(size, len(BLOB))
        self.assertEqual(ctype, "video/mp4")
        self.assertEqual(part.read_bytes(), BLOB)

    def test_resumes_after_a_dropped_connection_without_starting_over(self):
        self.server.drop_after = 300_000
        part = self.media / "t.part"
        fetch_to_file(lambda: self.server.url, part, sleep=lambda s: None)
        self.assertEqual(part.read_bytes(), BLOB)
        # The second request picks up where the saved part ended...
        m = re.match(r"bytes=(\d+)-$", self.server.requests[1])
        self.assertIsNotNone(m)
        resumed_at = int(m.group(1))
        self.assertGreater(resumed_at, 0)
        # ...so at most the piece being read when the link went away travels twice.
        self.assertLessEqual(self.server.bytes_sent - len(BLOB), 64 * 1024)
        self.assertEqual(resumed_at % (64 * 1024), 0)

    def test_each_attempt_uses_the_freshest_link(self):
        self.server.drop_after = 1000
        links = iter([self.server.url, self.server.url.replace("sig=1", "sig=2")])
        seen = []

        def provider():
            url = next(links, None) or self.server.url
            seen.append(url)
            return url

        fetch_to_file(provider, self.media / "t.part", sleep=lambda s: None)
        self.assertIn("sig=2", seen[1])


class DownloadMediaTests(_MediaTest):
    def test_downloads_into_place_only_when_whole(self):
        path, kind = handlers.download_media(self.server.url, "task-1")
        self.assertEqual((path, kind), ("signage/task-1.mp4", "video"))
        self.assertEqual((self.media / path).read_bytes(), BLOB)
        self.assertFalse(list((self.media / "signage").glob("*.part")))

    def test_keeps_a_complete_copy_instead_of_downloading_again(self):
        (self.media / "signage").mkdir()
        (self.media / "signage" / "task-1.mp4").write_bytes(BLOB)
        path, kind = handlers.download_media(self.server.url, "task-1")
        self.assertEqual((path, kind), ("signage/task-1.mp4", "video"))
        # Only the one-byte size probe was asked for.
        self.assertEqual(self.server.requests, ["bytes=0-0"])
        self.assertLessEqual(self.server.bytes_sent, 1)

    def test_downloads_again_when_the_local_copy_differs(self):
        (self.media / "signage").mkdir()
        (self.media / "signage" / "task-1.mp4").write_bytes(b"old")
        handlers.download_media(self.server.url, "task-1")
        self.assertEqual((self.media / "signage" / "task-1.mp4").read_bytes(), BLOB)


class NoDoubleDownloadTests(TransactionTestCase):
    def test_a_resend_while_downloading_keeps_the_newer_link_and_starts_nothing(self):
        downloads = TaskDownloads()
        release = threading.Event()
        runs = []

        def job(task_id):
            runs.append(downloads.latest(task_id)["url"])
            release.wait(5)

        self.assertTrue(downloads.submit("t", {"url": "a"}, job))
        time.sleep(0.1)
        self.assertFalse(downloads.submit("t", {"url": "b"}, job))
        self.assertEqual(downloads.latest("t")["url"], "b")
        release.set()
        downloads.shutdown()
        self.assertEqual(runs, ["a"])  # one job only
        self.assertFalse(downloads.in_flight("t"))


@override_settings(DEVICE_ID="")
class MqttThreadStaysFreeTests(_MediaTest):
    def test_a_task_message_returns_at_once_and_the_task_lands_later(self):
        self.server.delay = 1.0  # a slow link
        msg = {
            "DSMTaskId": "pr-1",
            "DSMId": "dsm-1",
            "name": "ประชาสัมพันธ์ (แบนเนอร์)",
            "type": "PUBLICRELATION",
            "url": {"url": self.server.url},
            "dateStartedAt": "2026-12-01",
            "dateEndAt": "2027-04-30",
            "timeStartedAt": "00:00:00",
            "timeEndAt": "23:59:59",
            "timezone": "Asia/Bangkok",
        }
        t0 = time.monotonic()
        handlers.handle_mqtt_message(None, "pat-sig/dev/data", json.dumps(msg))
        self.assertLess(time.monotonic() - t0, 0.5)  # did not wait for the file
        for _ in range(100):
            if not task_downloads.in_flight("pr-1"):
                break
            time.sleep(0.05)
        task = DSMTask.objects.get(dsm_task_id="pr-1")
        self.assertEqual(task.status, "downloaded")
        self.assertEqual(task.media_local_path, "signage/pr-1.mp4")
        self.assertEqual(str(task.date_end_at), "2027-04-30")


@override_settings(DEVICE_ID="")
class AlertOverridesPrTests(TransactionTestCase):
    """Carey 2026-11-20: an alert must always be able to override the PR banner."""

    def setUp(self):
        today = timezone.localdate()
        self.pr = DSMTask.objects.create(
            dsm_task_id="pr-1",
            dsm_id="dsm-1",
            name="ประชาสัมพันธ์ (แบนเนอร์)",
            task_type="PUBLICRELATION",
            media_local_path="signage/pr-1.mp4",
            media_type="video",
            timezone="Asia/Bangkok",
            date_started_at=today - timedelta(days=1),
            date_end_at=today + timedelta(days=30),
            time_started_at="00:00:00",
            time_end_at="23:59:59",
            status="playing",
        )
        self.alert = DSMTask.objects.create(
            dsm_task_id="alert-1",
            dsm_id="dsm-1",
            name="เตือนภัยระดับต่ำ",
            task_type="ALERTLOW",
            media_local_path="signage/alert-1.png",
            media_type="image",
            status="downloaded",
        )

    def _screen(self):
        res = Client().get("/", HTTP_ACCEPT="application/json")
        return res.json()["media_url"]

    def test_an_alert_takes_the_screen_from_a_playing_pr(self):
        handlers.handle_alert_message(json.dumps({"DSMTaskId": "alert-1"}))
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.status, "downloaded")  # paused, not dropped
        self.assertTrue(self._screen().endswith("signage/alert-1.png"))

    def test_pr_does_not_restart_while_the_alert_plays_and_resumes_after(self):
        from home.services.scheduler_service import scheduler_service

        handlers.handle_alert_message(json.dumps({"DSMTaskId": "alert-1"}))
        scheduler_service._start_tasks()
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.status, "downloaded")

        handlers.handle_alert_message(
            json.dumps({"DSMTaskId": "alert-1", "action": "clear"})
        )
        scheduler_service._start_tasks()
        self.pr.refresh_from_db()
        self.assertEqual(self.pr.status, "playing")
        self.assertTrue(self._screen().endswith("signage/pr-1.mp4"))
