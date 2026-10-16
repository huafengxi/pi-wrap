"""Local workspace-root contract v1 (source: agent-platform/platform_paths.py).

Source identity follows realpath, not cwd or symlink ancestors. The caller may
supply PLATFORM_WORKSPACE; portable deliberately implements a narrower contract.
No marker, sibling repository, or platform installation is consulted.
"""
import os
from pathlib import Path
import sys


def workspace_root(source):
    """Return nonempty override (expanded/absolute), else source repo's parent."""
    override = os.environ.get("PLATFORM_WORKSPACE")
    if override:
        return str(Path(override).expanduser().resolve())
    return str(Path(source).resolve().parent.parent)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: platform_paths.py <component entry path>")
    print(workspace_root(sys.argv[1]))
