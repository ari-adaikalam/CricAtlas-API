"""One run at a time. A lock file held with flock: the lock goes away by itself when the process ends, however it ends, so there is no stale lock to clean up."""
import fcntl
import io
import os
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


class RunLock:
    def __init__(self, path: Path, what: str):
        self.path, self.what, self.handle = path, what, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.seek(0)
            holder = self.handle.read().strip()
            self.handle.close()
            raise AlreadyRunning(f"another {self.what} is running{' (' + holder + ')' if holder else ''}. Wait for it, or stop it, then try again.") from None
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(f"pid {os.getpid()}")
        self.handle.flush()
        return self

    @classmethod
    def adopt(cls, path: Path, what: str, descriptor: int):
        """Take over a lock that the process before an exec still holds, through the descriptor it kept open. The lock is never released in between, so no other run can start.
        A descriptor that is not this lock file, or that does not hold the lock, is refused."""
        lock = cls(path, what)
        try:
            lock.handle = io.FileIO(descriptor, "r+", closefd=False)      # unbuffered, so closing it never touches a descriptor its owner has already closed
        except OSError:
            raise AlreadyRunning(f"the {what} lock was not handed over") from None
        try:
            held, wanted = os.fstat(descriptor), os.stat(path)
            if (held.st_dev, held.st_ino) != (wanted.st_dev, wanted.st_ino):
                raise AlreadyRunning(f"the {what} lock was not handed over")
            try:
                fcntl.flock(lock.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)     # the same open file already holds it, so this succeeds; anything else fails
            except OSError:
                raise AlreadyRunning(f"another {what} is running") from None
        except (OSError, AlreadyRunning) as error:
            lock.handle.close()                  # closefd is off, so this leaves the descriptor itself to its owner
            lock.handle = None
            raise error if isinstance(error, AlreadyRunning) else AlreadyRunning(f"the {what} lock was not handed over") from None
        return lock

    def __exit__(self, *exc):
        if self.handle:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
