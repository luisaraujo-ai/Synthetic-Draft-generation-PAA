"""One writer per domain across generation/recovery commands (macOS and Linux)."""
import atexit
from contextlib import contextmanager
import fcntl


def acquire_writer_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise SystemExit(f'Another pipeline writer holds {path}; stop it before starting this command.')
    atexit.register(handle.close)
    return handle


@contextmanager
def coordinator_lock(path):
    handle = acquire_writer_lock(path)
    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
