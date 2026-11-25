"""
Writing migration files: each through a temporary file and a rename, keeping
its mode, and all restored if the result doesn't verify.
"""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


class WriteError(Exception):
    pass


@dataclass(frozen=True)
class Planned:
    """A file to replace with ``new`` (bytes), or to delete (``new`` None)."""

    path: Path
    new: bytes | None


def check_writable(paths: Sequence[Path]) -> None:
    """Refuse, before anything is written, files that can't be replaced as
    they are: read-only files, symbolic links and non-regular files."""
    problems: list[str] = []
    for path in paths:
        try:
            info = path.lstat()
        except OSError as exc:
            problems.append(f"{path} ({exc.strerror})")
            continue
        if stat.S_ISLNK(info.st_mode):
            problems.append(f"{path} (a symbolic link)")
        elif not stat.S_ISREG(info.st_mode):
            problems.append(f"{path} (not a regular file)")
        elif not os.access(path, os.W_OK) or not info.st_mode & stat.S_IWUSR:
            problems.append(f"{path} (read-only)")
        elif not os.access(path.parent, os.W_OK | os.X_OK):
            problems.append(f"{path} (its directory is read-only)")
    if problems:
        raise WriteError(
            "These migration files can't be written, so nothing was written: "
            + ", ".join(problems)
            + "."
        )


def _replace(path: Path, data: bytes, mode: int) -> None:
    """Replace ``path`` with ``data`` through a temporary file and a rename."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_files(files: Sequence[Planned], verify: Callable[[], None]) -> None:
    """
    Write ``files``, then call ``verify``; if anything fails (or is
    interrupted), restore every file as it was, with its mode, and re-raise.
    """
    check_writable([f.path for f in files])
    modes = {f.path: stat.S_IMODE(f.path.stat().st_mode) for f in files}
    originals = {f.path: f.path.read_bytes() for f in files}
    touched: list[Path] = []
    try:
        for planned in files:
            # Registered before writing: a failure mid-write must still
            # restore that file.
            touched.append(planned.path)
            if planned.new is None:
                planned.path.unlink()
            else:
                _replace(planned.path, planned.new, modes[planned.path])
        verify()
    except BaseException as exc:
        unrestored: list[str] = []
        for path in touched:
            try:
                _replace(path, originals[path], modes[path])
            except OSError as restore_exc:
                unrestored.append(f"{path} ({restore_exc})")
        if unrestored:
            raise WriteError(
                f"Writing the migration files failed ({exc!r}) and these could"
                f" not be restored: {', '.join(unrestored)}. Restore them"
                " with Git."
            ) from exc
        raise
