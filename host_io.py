"""Small local host-record I/O, not a task/file protocol implementation."""
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile


def now_ts():
    return datetime.now().astimezone().isoformat(timespec='milliseconds')


def read_owner(path):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None
    # A malformed/unreadable owner is unknown, not proof its endpoint is stale.


def write_owner(path, record):
    target = Path(path)
    fd, name = tempfile.mkstemp(prefix=target.name + '.', dir=target.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(record, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, target)
    finally:
        Path(name).unlink(missing_ok=True)
