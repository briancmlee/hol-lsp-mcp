"""How much memory a server process uses, and the default limits.

The measure is the physical footprint (what macOS's `footprint` and top's
MEM report), not RSS: a Poly/ML heap that has been pushed out to the
compressor or to swap still counts.  A `hol lsp` has been seen at 1 GB RSS
and 13 GB footprint.

Two limits:
  hard: Poly/ML's --maxheap for each server.  Poly's own default is 80%
        of RAM, so one looping proof can take the whole machine.  At the
        cap Poly interrupts every thread: the proof fails with Interrupt,
        or, if Poly cannot recover ("Run out of store ... Failed to recover
        - exiting"), the server exits and the next call restarts it.
  soft: a footprint above which `diagnostics` says the server is probably
        running a looping proof.  It only reports; nothing is stopped.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import struct
import sys

_libproc = None


def footprint_mb(pid: int) -> int | None:
    """Physical footprint of `pid` in MB, or None if it can't be read."""
    global _libproc
    try:
        if sys.platform == "darwin":
            if _libproc is None:
                _libproc = ctypes.CDLL(ctypes.util.find_library("proc")
                                       or "/usr/lib/libproc.dylib")
            # proc_pid_rusage(pid, RUSAGE_INFO_V0, &info): ri_phys_footprint
            # follows a 16-byte uuid and seven uint64 fields.
            buf = ctypes.create_string_buffer(256)
            if _libproc.proc_pid_rusage(int(pid), 0, buf) != 0:
                return None
            return struct.unpack_from("<Q", buf, 72)[0] // (1 << 20)
        with open(f"/proc/{int(pid)}/status") as f:
            kb = sum(int(line.split()[1]) for line in f
                     if line.startswith(("VmRSS:", "VmSwap:")))
        return kb // 1024
    except (OSError, ValueError, AttributeError):
        return None


# A normal server stays under about 2 GB, whatever the machine, so the
# defaults are fixed rather than scaled with RAM.  A file that genuinely
# needs more (CakeML translator scripts, say) gets it from
# `restart(file, max_heap_mb=...)`.
DEFAULT_MAX_HEAP_MB = 3000
DEFAULT_SOFT_MB = 2000


def default_max_heap_mb() -> int:
    return DEFAULT_MAX_HEAP_MB


def default_soft_mb() -> int:
    return DEFAULT_SOFT_MB
