"""Single-process durable outbox worker for NexAlert.

The application server only commits and queues SOS records. Run this worker as
one separately supervised process; SQLite leases/process-local locking are not
a supported multi-worker coordination service.
"""
from __future__ import annotations

import argparse
import os
import time


def process_one() -> int:
    """Attempt at most one due record. Deterministic and callable by tests."""
    try:
        from backend import app as service
    except ModuleNotFoundError:
        import app as service
    service.init_db()
    return service.transport.process_queue(limit=1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="process at most one due incident")
    parser.add_argument("--drain-once", action="store_true", help="process one bounded batch then exit")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args()
    if args.poll_seconds < 0.1 or args.poll_seconds > 60:
        parser.error("--poll-seconds must be between 0.1 and 60")
    try:
        from backend import app as service
    except ModuleNotFoundError:
        import app as service
    service.init_db()
    lock_path = os.environ.get("NEXALERT_WORKER_LOCK", service.DB_PATH + ".worker.lock")
    with open(lock_path, "a+") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write("0"); lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise SystemExit("another NexAlert outbox worker holds the lock") from exc
        else:
            import fcntl
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SystemExit("another NexAlert outbox worker holds the lock") from exc
        while True:
            service.transport.process_queue(limit=1 if args.once else 500)
            if args.once or args.drain_once:
                break
            time.sleep(args.poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
