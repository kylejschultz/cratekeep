"""Remove empty Inbox directories only, using no-follow directory descriptors."""
import errno
import os
from pathlib import Path


def prune_empty_directories(root: Path, *, scopes=None, protected=()):
    removed, errors = [], []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    protected = set(protected)
    wanted = None if scopes is None else set(scopes)

    def walk(parent_fd, name, relative):
        descriptor = None
        try:
            descriptor = os.open(name, flags, dir_fd=parent_fd)
            identity = os.fstat(descriptor)
            with os.scandir(descriptor) as entries:
                children = [entry.name for entry in entries if entry.is_dir(follow_symlinks=False)]
            for child in children:
                walk(descriptor, child, relative / child)
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
                raise OSError("Directory changed during cleanup")
            # rmdir can never remove audio, artwork, hidden files or symlinks.
            os.rmdir(name, dir_fd=parent_fd)
            removed.append(relative.as_posix())
        except OSError as exc:
            if exc.errno not in {errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT, errno.ELOOP, errno.ENOTDIR}:
                errors.append({"path": relative.as_posix(), "error": str(exc)})
        finally:
            if descriptor is not None:
                os.close(descriptor)

    descriptor = os.open(root, flags)
    try:
        with os.scandir(descriptor) as entries:
            children = [entry.name for entry in entries if entry.is_dir(follow_symlinks=False)]
        for name in children:
            if name not in protected and (wanted is None or name in wanted):
                walk(descriptor, name, Path(name))
    finally:
        os.close(descriptor)
    return {"removed": removed, "errors": errors}
