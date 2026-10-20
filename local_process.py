"""Local process and Unix peer facts; no agent task/bot policy.

Linux start tokens are unchanged /proc ticks. Darwin tokens are integer
microseconds from proc_bsdinfo's start timeval. These are local identity checks,
not atomic process handles. Permission/ABI/parse errors are never absence.
"""
import ctypes
import errno
from functools import lru_cache
import os
from pathlib import Path
import socket
import struct
import sys
import uuid


class BSDInfo(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint32) for n in (
        'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid',
        'rgid', 'svuid', 'svgid', 'reserved')] + [
        ('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)] + [
        (n, ctypes.c_uint32) for n in ('nfiles', 'pgid', 'pjobc', 'e_tdev', 'e_tpgid')] + [
        ('nice', ctypes.c_int32), ('start_sec', ctypes.c_uint64),
        ('start_usec', ctypes.c_uint64)]


@lru_cache(maxsize=1)
def _darwin():
    if sys.platform != 'darwin' or ctypes.sizeof(BSDInfo) != 136:
        raise RuntimeError('unsupported Darwin proc_bsdinfo ABI')
    lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                 ctypes.c_void_p, ctypes.c_int]
    lib.proc_pidinfo.restype = ctypes.c_int
    lib.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.c_void_p, ctypes.c_int]
    lib.proc_listpids.restype = ctypes.c_int
    return lib


@lru_cache(maxsize=1)
def boot_id():
    if sys.platform.startswith('linux'):
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if sys.platform != 'darwin':
        raise RuntimeError('unsupported local process platform')
    # kern.boottime is a wall-clock estimate and changes on clock correction.
    # XNU exposes a stable UUID for this boot session; never fall back to time.
    libc = ctypes.CDLL(None, use_errno=True)
    fn = libc.sysctlbyname
    fn.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                   ctypes.c_void_p, ctypes.c_size_t]
    fn.restype = ctypes.c_int
    value = ctypes.create_string_buffer(37)
    size = ctypes.c_size_t(len(value))
    if fn(b'kern.bootsessionuuid', value, ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), 'kern.bootsessionuuid')
    if size.value != 37 or value.raw[-1:] != b'\0':
        raise RuntimeError('invalid kern.bootsessionuuid size')
    try:
        token = value.raw[:-1].decode('ascii')
        parsed = uuid.UUID(token)
    except (ValueError, UnicodeDecodeError) as error:
        raise RuntimeError('invalid kern.bootsessionuuid') from error
    if str(parsed) != token.lower() or not parsed.int:
        raise RuntimeError('invalid kern.bootsessionuuid')
    return 'darwin:' + str(parsed)



def _bsd_info(pid):
    value = BSDInfo()
    ctypes.set_errno(0)
    n = _darwin().proc_pidinfo(pid, 3, 0, ctypes.byref(value), ctypes.sizeof(value))
    if n != ctypes.sizeof(value):
        err = ctypes.get_errno()
        if n == 0 and err == errno.ESRCH:
            return None
        raise OSError(err or errno.EIO, 'proc_pidinfo incomplete/denied')
    if value.pid != pid or value.start_sec <= 0 or value.start_usec >= 1000000:
        raise RuntimeError('invalid proc_bsdinfo identity')
    return value


def identity(pid):
    if type(pid) is not int or pid <= 0:
        raise ValueError('positive integer pid required')
    if sys.platform.startswith('linux'):
        try:
            raw = Path('/proc/%d/stat' % pid).read_text()
            uid = os.stat('/proc/%d' % pid).st_uid
        except FileNotFoundError:
            return None
        fields = raw[raw.rfind(')') + 2:].split()
        return dict(pid=pid, start=fields[19], boot=boot_id(), uid=uid,
                    pgid=int(fields[2]), sid=int(fields[3]),
                    ppid=int(fields[1]), state=fields[0])
    if sys.platform != 'darwin':
        raise RuntimeError('unsupported local process platform')
    b = _bsd_info(pid)
    if b is None:
        return None
    # getsid is a separate observation, not an atomic handle. Recheck start so
    # a detected reuse during observation is an error rather than mixed facts.
    try:
        sid = os.getsid(pid)
    except ProcessLookupError:
        return None
    after = _bsd_info(pid)
    if after is None:
        return None
    if (b.start_sec, b.start_usec, b.pgid, b.uid) != (
            after.start_sec, after.start_usec, after.pgid, after.uid):
        raise RuntimeError('process changed during identity read')
    states = {1: 'I', 2: 'R', 3: 'S', 4: 'T', 5: 'Z'}
    if after.status not in states:
        raise RuntimeError('unknown Darwin process state')
    return dict(pid=pid, start=str(after.start_sec * 1000000 + after.start_usec),
                boot=boot_id(), uid=after.uid, pgid=after.pgid, sid=sid,
                ppid=after.ppid, state=states[after.status])


def starttime(pid):
    value = identity(pid)
    return int(value['start']) if value is not None else None


def owner_alive(pid, expected_start):
    value = identity(pid)
    # Legacy metadata without a start cannot authorize takeover of a live PID.
    return value is not None and (expected_start is None or
                                  value['start'] == str(expected_start))


def _pids():
    if sys.platform.startswith('linux'):
        return [int(p.name) for p in Path('/proc').iterdir() if p.name.isdecimal()]
    lib = _darwin()
    # Enumerate only our uid; foreign group members cannot be cooperative peers.
    size = lib.proc_listpids(4, os.getuid(), None, 0)
    if size <= 0:
        raise OSError(ctypes.get_errno() or errno.EIO, 'proc_listpids size')
    for _ in range(4):
        values = (ctypes.c_int * (size // ctypes.sizeof(ctypes.c_int) + 128))()
        capacity = ctypes.sizeof(values)
        n = lib.proc_listpids(4, os.getuid(), values, capacity)
        if n <= 0 or n % ctypes.sizeof(ctypes.c_int):
            raise OSError(ctypes.get_errno() or errno.EIO, 'proc_listpids data')
        if n < capacity:
            return [pid for pid in list(values)[:n // ctypes.sizeof(ctypes.c_int)] if pid > 0]
        size = capacity * 2
    raise RuntimeError('process enumeration did not converge')


def group_members(host):
    result = []
    for pid in _pids():
        value = identity(pid)
        if (value and value['state'] not in ('Z', 'X') and
                value['pgid'] == host['pgid'] and value['sid'] == host['sid']):
            result.append(value)
    return result


def peer_credentials(sock):
    """Kernel server PID/uid/gid for this connected AF_UNIX socket."""
    if sys.platform.startswith('linux'):
        return struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if sys.platform != 'darwin':
        raise RuntimeError('unsupported socket peer platform')
    # sys/un.h: SOL_LOCAL=0, LOCAL_PEERPID=2. getpeereid supplies effective uid/gid.
    pid, = struct.unpack('i', sock.getsockopt(0, 2, struct.calcsize('i')))
    uid, gid = ctypes.c_uint(), ctypes.c_uint()
    fn = ctypes.CDLL(None, use_errno=True).getpeereid
    fn.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
    fn.restype = ctypes.c_int
    if fn(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
        raise OSError(ctypes.get_errno(), 'getpeereid')
    if pid <= 0:
        raise RuntimeError('invalid socket peer PID')
    return pid, uid.value, gid.value
