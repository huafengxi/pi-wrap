"""Locate explicitly configured agentd or a real-source sibling; no platform fallback.

Current dependency/interface pin is recorded in recovery/fresh-dependencies.json.
Historical dependency records remain frozen for their original candidates.
This locator does not implement protocol, lifecycle, envscrub or wait state.
"""
import importlib
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
                          "recovery/fresh-dependencies.json. No unified-repo fallback." %
                          (", ".join(missing), root,
                           "explicit AGENTD_DIR" if explicit else "development sibling"))
    path = str(root)
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)
    # Do not accept a cached module from another checkout (including a caller's
    # platform checkout). A path entry alone does not establish import origin.
    for name in modules:
        module = importlib.import_module(name)
        if Path(module.__file__).resolve() != (root / (name + '.py')).resolve():
            raise ImportError('pi-wrap refuses foreign cached agentd module %s: %s; expected %s' %
                              (name, module.__file__, root))
        if name == 'wait_state':
            cls = getattr(module, 'DialogRequests', None)
            if (not callable(cls) or
                    any(not callable(getattr(cls, method, None)) for method in ('event', 'sent', 'dialogs')) or
                    set(getattr(module, 'DIALOG_METHODS', ())) != {'select', 'confirm', 'input', 'editor'}):
                raise ImportError('pi-wrap requires agentd wait_state.DialogRequests '
                                  '(event/sent/dialogs) and DIALOG_METHODS at %s; '
                                  'old CurrentWaits is not a compatible fallback. '
                                  'See recovery/fresh-dependencies.json.' % root)
    return path
