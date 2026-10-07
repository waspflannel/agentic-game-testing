"""Prevent two local controllers/watchdogs from driving the same data store."""
import os
from pathlib import Path


def acquire(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        # Windows byte-range locks can reject this read before locking() runs.
        # Close the handle and report the same ownership failure in either case.
        stream.seek(0)
        if stream.read(1) == b"":
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise RuntimeError(f"Another ASHAD process owns {path.name}") from None
    return stream
