"""Download a task's media to disk: streaming, resumable, never in memory.

The backend can send files of up to 2 GB (the PR promo clip). The old download
read the whole body into RAM (``response.content``) — fatal on a board with
~2.8 GB free — and a 4G drop meant starting over. Here the body is streamed in
64 KB pieces into ``<task>.part``; after a drop the next attempt asks for the
rest with an HTTP Range request (the storage serves ranges), and only a file
whose size matches what the server announced is renamed into place. Until then
nothing the screen uses changes.

Every attempt gets a fresh link from ``url_provider``: the backend re-sends a
pending task (with a new presigned link) every 10 s, and a link is valid 24 h.
"""
from __future__ import annotations  # PEP 604 (X | None) on Python 3.9

import logging
import re
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# Small pieces on purpose: when a connection drops, the HTTP library discards
# the piece it was reading, so this is the most a 4G drop can cost.
CHUNK = 64 * 1024
CONNECT_TIMEOUT_S = 15
READ_TIMEOUT_S = 60  # no byte for this long = the connection is dead; resume
GIVE_UP_AFTER_S = 24 * 3600  # a presigned link lives 24 h
BACKOFF_MAX_S = 60

_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)")


class DownloadError(Exception):
    pass


def _total_from(resp: requests.Response, offset: int) -> Optional[int]:
    """Full size of the object, from Content-Range (206) or Content-Length (200)."""
    cr = resp.headers.get("Content-Range")
    if cr:
        m = _CONTENT_RANGE.match(cr)
        if m and m.group(3) != "*":
            return int(m.group(3))
    cl = resp.headers.get("Content-Length")
    if cl is not None and cl.isdigit():
        return offset + int(cl)
    return None


def remote_size(url: str, session=requests) -> Optional[int]:
    """Size of the object behind a presigned GET link, or None if unknown.

    HEAD is not allowed on a link signed for GET, so this asks for the first
    byte only and reads the total from Content-Range.
    """
    try:
        with session.get(
            url,
            headers={"Range": "bytes=0-0"},
            stream=True,
            timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
        ) as resp:
            if resp.status_code == 206:
                return _total_from(resp, 0)
            if resp.status_code == 200:
                return _total_from(resp, 0)  # range ignored: Content-Length
    except requests.RequestException as e:
        logger.warning(f"Size probe failed: {e}")
    return None


def fetch_to_file(
    url_provider: Callable[[], str],
    part: Path,
    *,
    session=requests,
    give_up_after_s: float = GIVE_UP_AFTER_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Tuple[int, Optional[str]]:
    """Fill ``part`` with the whole object, resuming after drops.

    Returns (size, content_type). Raises DownloadError when the object cannot be
    had before ``give_up_after_s``. The caller renames ``part`` into place.
    """
    deadline = clock() + give_up_after_s
    backoff = 2.0
    content_type: Optional[str] = None
    total: Optional[int] = None
    attempt = 0
    while True:
        attempt += 1
        have = part.stat().st_size if part.exists() else 0
        if total is not None and have == total:
            return have, content_type
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with session.get(
                url_provider(),
                headers=headers,
                stream=True,
                timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
            ) as resp:
                if resp.status_code == 416 and have:
                    # Nothing left to send: the part is complete — or bigger
                    # than the object (it changed). Check against the size.
                    size = remote_size(url_provider(), session)
                    if size is not None and size == have:
                        return have, content_type
                    part.unlink()
                    continue
                if resp.status_code == 206:
                    m = _CONTENT_RANGE.match(resp.headers.get("Content-Range", ""))
                    if not m or int(m.group(1)) != have:
                        # Not the piece we asked for: start over cleanly.
                        part.unlink(missing_ok=True)
                        continue
                    mode = "ab"
                elif resp.status_code == 200:
                    mode = "wb"  # the server sent the whole thing from 0
                    have = 0
                else:
                    resp.raise_for_status()
                    raise DownloadError(f"unexpected HTTP {resp.status_code}")
                content_type = resp.headers.get("Content-Type") or content_type
                total = _total_from(resp, have) or total
                with open(part, mode) as f:
                    for piece in resp.iter_content(CHUNK):
                        if piece:
                            f.write(piece)
            have = part.stat().st_size
            if total is None or have == total:
                return have, content_type
            logger.warning(
                f"Download of {part.name} ended short ({have}/{total} bytes); resuming"
            )
            backoff = 2.0  # it was moving: retry quickly
        except (requests.RequestException, OSError) as e:
            have = part.stat().st_size if part.exists() else 0
            logger.warning(
                f"Download of {part.name} interrupted at {have} bytes "
                f"(attempt {attempt}): {e}"
            )
        if clock() >= deadline:
            raise DownloadError(
                f"gave up on {part.name} after {give_up_after_s:.0f} s at "
                f"{part.stat().st_size if part.exists() else 0} bytes"
            )
        sleep(backoff)
        backoff = min(backoff * 2, BACKOFF_MAX_S)


def complete_local_file(media_dir: Path, dsm_task_id: str) -> Optional[Path]:
    """The finished file of this task already on the sign (any extension), if any."""
    for p in media_dir.glob(f"{dsm_task_id}.*"):
        if p.suffix != ".part" and p.is_file():
            return p
    return None


def cleanup_stale_parts(media_dir: Path, older_than_s: float = GIVE_UP_AFTER_S) -> int:
    """Delete .part files nobody can finish any more (older than a link's life)."""
    removed = 0
    now = time.time()
    for p in media_dir.glob("*.part"):
        try:
            if now - p.stat().st_mtime > older_than_s:
                p.unlink()
                removed += 1
        except OSError:
            pass
    return removed
