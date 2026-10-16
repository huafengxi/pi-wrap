"""Deletion authority is only this process's newly created fixture base."""
from pathlib import Path
import tempfile
from agentd_dependency import require_agentd
require_agentd("safe_delete")
from safe_delete import safe_rmtree

# Short independent root keeps unix socket tests below sun_path limits.
BASE = Path(tempfile.mkdtemp(prefix="pw-", dir="/tmp"))


def fixture_dir(prefix="fixture-", dir=None):
    return tempfile.mkdtemp(prefix=prefix, dir=BASE)


def cleanup_tree(path):
    deleted, reason = safe_rmtree(path, authorized_root=BASE, allow_outside_temp=(BASE,))
    if not deleted:
        raise AssertionError(reason)
