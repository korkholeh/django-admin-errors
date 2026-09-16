"""`BEFORE_SEND` hook that makes the demo's payloads readable, and its screenshots portable.

Frame filenames are absolute, so every traceback in the demo admin — and every README screenshot
taken from it — carries the checkout's location, which on a developer machine means a home
directory and a username. Nothing is hidden: the paths are rewritten relative to the repository
root (`/Users/someone/src/django-admin-errors/demo/demo_app/views.py` becomes
`demo/demo_app/views.py`, and a dependency becomes `.venv/lib/python3.11/site-packages/...`),
which is also simply shorter to read.

The server hostname is replaced for the same reason: `socket.gethostname()` is whatever the
machine happens to be called, which is noise in a screenshot and differs on every machine the
demo runs on. `DEMO_HOSTNAME` keeps that block stable and anonymous.

This is not library behaviour, and `admin_errors` ships nothing like it: it is the demo using the
documented `BEFORE_SEND` extension point (spec section 5) on itself, which doubles as the worked
example of that hook. It runs after the frames are built, so `in_app` has already been decided
from the real paths and the rewrite cannot change which frames the admin highlights
(`admin_errors/capture.py`, `_apply_before_send`).
"""

from pathlib import Path
from typing import Any

REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent) + "/"
DEMO_HOSTNAME = "demo-web-1"


def _shorten(filename: Any) -> Any:
    if isinstance(filename, str) and filename.startswith(REPO_ROOT):
        return filename[len(REPO_ROOT) :]
    return filename


def _shorten_frames(frames: Any) -> None:
    if not isinstance(frames, list):
        return
    for frame in frames:
        if isinstance(frame, dict) and "filename" in frame:
            frame["filename"] = _shorten(frame["filename"])


def tidy_payload(payload: dict, hint: dict) -> dict:
    """Shorten absolute frame filenames and pin the reported hostname."""
    _shorten_frames(payload.get("frames"))
    exception = payload.get("exception")
    if isinstance(exception, dict):
        for link in exception.get("chain") or []:
            if isinstance(link, dict):
                _shorten_frames(link.get("frames"))

    server = payload.get("server")
    if isinstance(server, dict) and "hostname" in server:
        server["hostname"] = DEMO_HOSTNAME
    return payload
