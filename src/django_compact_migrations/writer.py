"""
Writing migration files so that an interrupted write can be undone.

Before any file changes, the original of every file to be written or deleted
is copied into a journal directory at the project root. The files are then
replaced one by one (each through a temporary file and a rename, keeping its
mode). The journal is removed once the result is verified or every file is
restored. If the process is killed in between, the next run finds the
journal and restores the files it changed (see ``recover``).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from django_compact_migrations import worktree

JOURNAL = ".compact-migrations-journal"


class WriteError(Exception):
    pass


@dataclass(frozen=True)
class Planned:
    """A file to replace with ``new`` (bytes), or to delete (``new`` None)."""

    path: Path
    new: bytes | None


def journal_dir(root: Path | None = None) -> Path:
    return (root or worktree.project_root()) / JOURNAL


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


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover - not on Windows
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - some file systems
        pass
    finally:
        os.close(fd)


class Journal:
    """The originals of the files being written, until they are verified."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @classmethod
    def start(cls, files: Sequence[Planned], root: Path | None = None) -> Journal:
        directory = journal_dir(root)
        if directory.exists():
            raise WriteError(
                f"{directory} exists: an earlier apply was interrupted. Run the"
                " command again to restore the files it changed."
            )
        tmp = Path(tempfile.mkdtemp(dir=directory.parent, prefix=f"{JOURNAL}."))
        entries = []
        for number, planned in enumerate(files):
            original = planned.path.read_bytes()
            (tmp / f"{number}.orig").write_bytes(original)
            entries.append(
                {
                    "path": os.fspath(planned.path.resolve()),
                    "original": f"{number}.orig",
                    "mode": stat.S_IMODE(planned.path.stat().st_mode),
                    "sha256_before": _sha256(original),
                    "sha256_after": None
                    if planned.new is None
                    else _sha256(planned.new),
                }
            )
        (tmp / "journal.json").write_text(json.dumps(entries, indent=2))
        _fsync_dir(tmp)
        # The rename makes the journal complete at once.
        os.replace(tmp, directory)
        _fsync_dir(directory.parent)
        return cls(directory)

    def finish(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


def write_files(
    files: Sequence[Planned], verify: Callable[[], None], root: Path | None = None
) -> None:
    """
    Write ``files``, then call ``verify``; if anything fails (or is
    interrupted), restore every file as it was, with its mode, and re-raise.
    """
    check_writable([f.path for f in files])
    modes = {f.path: stat.S_IMODE(f.path.stat().st_mode) for f in files}
    originals = {f.path: f.path.read_bytes() for f in files}
    journal = Journal.start(files, root)
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
                f" not be restored: {', '.join(unrestored)}. Their originals"
                f" are in {journal.directory}."
            ) from exc
        journal.finish()
        raise
    journal.finish()


def recover(root: Path | None = None) -> list[str]:
    """
    Restore the files an interrupted write changed, from its journal, and
    say what was done. A file changed since by something else is left as it
    is, and so is the journal then.
    """
    directory = journal_dir(root)
    manifest = directory / "journal.json"
    if not manifest.exists():
        return []
    entries = json.loads(manifest.read_text())
    messages: list[str] = []
    kept = False
    for entry in entries:
        path = Path(entry["path"])
        original = (directory / entry["original"]).read_bytes()
        try:
            current: bytes | None = path.read_bytes()
        except FileNotFoundError:
            current = None
        if current is not None and _sha256(current) == entry["sha256_before"]:
            continue  # never written
        written = (
            current is None
            if entry["sha256_after"] is None
            else current is not None and _sha256(current) == entry["sha256_after"]
        )
        if not written:
            kept = True
            messages.append(
                f"{path} changed since the interrupted apply, so it was left"
                f" as it is; its original is in {directory / entry['original']}."
            )
            continue
        _replace(path, original, entry["mode"])
        messages.append(f"Restored {path}.")
    if kept:
        messages.append(f"Remove {directory} once you have dealt with these.")
    else:
        shutil.rmtree(directory)
    return messages
