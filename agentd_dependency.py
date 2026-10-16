"""Locate explicitly configured agentd or a real-source sibling; no platform fallback.

Release dependency/interface pin is recorded in recovery/dependencies.json.
This locator does not implement protocol, lifecycle, envscrub or wait state.
"""
import os
from pathlib import Path
import sys


def require_agentd(*modules):
    explicit = os.environ.get("AGENTD_DIR")
    root = (Path(explicit).expanduser().resolve() if explicit else
            Path(__file__).resolve().parent.parent / "agentd")
    missing = [name + ".py" for name in modules if not (root / (name + ".py")).is_file()]
    if missing:
        raise ImportError("pi-wrap requires agentd interfaces %s at %s (%s). "
                          "Set AGENTD_DIR to a compatible checkout; see "
                          "recovery/dependencies.json. No unified-repo fallback." %
                          (", ".join(missing), root,
                           "explicit AGENTD_DIR" if explicit else "development sibling"))
    path = str(root)
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)
    return path
