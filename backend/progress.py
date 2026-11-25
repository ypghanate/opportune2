"""Live progress for the page. The tool server (a separate process) writes a small JSON file as it works, and the web app reads it
when the page asks. One file per user and kind ("search" or "plan"), in the temp folder."""
import json
import os
import tempfile
import time
from pathlib import Path

DIR = Path(os.environ.get("PROGRESS_DIR") or Path(tempfile.gettempdir()) / "opportune_progress")


def _path(user_id, kind: str) -> Path:
    return DIR / f"{int(user_id)}-{kind}.json"


def clear(user_id, kind: str) -> None:
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        _path(user_id, kind).unlink(missing_ok=True)
    except Exception:
        pass


def write(kind: str, data: dict) -> None:
    """Called from the tool server. Never raises: progress is a nicety and must not break the work."""
    try:
        user_id = os.environ.get("PROGRESS_USER")
        if not user_id:
            return
        DIR.mkdir(parents=True, exist_ok=True)
        target = _path(user_id, kind)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps({**data, "at": time.time()}, default=str))
        os.replace(tmp, target)  # atomic: the reader never sees half a file
    except Exception:
        pass


def read(user_id, kind: str) -> dict:
    try:
        return json.loads(_path(user_id, kind).read_text())
    except Exception:
        return {}