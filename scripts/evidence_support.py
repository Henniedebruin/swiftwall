"""Private, descriptor-relative evidence storage shared by supervised tooling."""
from contextlib import contextmanager
import os
from pathlib import Path
import stat


@contextmanager
def private_evidence_directory(path):
    """Open without following symlinks, and secure even an existing directory."""
    path = Path(path).absolute()
    if path == Path("/") or ".." in path.parts:
        raise ValueError("Unsafe evidence directory")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        if os.fstat(fd).st_uid != os.getuid():
            raise ValueError("Evidence directory must be owned by the current user")
        os.fchmod(fd, 0o700)
        yield fd
    finally:
        os.close(fd)


def private_evidence_log(directory, *, create, name="events.jsonl"):
    flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT
    try:
        fd = os.open(name, flags, 0o600, dir_fd=directory)
    except FileNotFoundError:
        if not create:
            return None
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise ValueError("Evidence log must be a private, singly linked regular file")
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a")
    except BaseException:
        os.close(fd)
        raise
