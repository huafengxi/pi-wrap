"""Linux exec trampoline: the ordinary pi child cannot outlive its P parent.

Runs in a fresh interpreter, avoiding preexec_fn in a threaded Python process.
The parent check on both sides of prctl closes the death-before-arming window.
"""
import ctypes
import os
import signal
import sys


def main():
    parent = int(sys.argv[1])
    if os.getppid() != parent:
        return 125
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), 'PR_SET_PDEATHSIG')
    if os.getppid() != parent:
        return 125
    os.execvpe(sys.argv[2], sys.argv[2:], os.environ)


if __name__ == '__main__':
    sys.exit(main())
