"""
Running project code in a forked child process.

Every migrate run on a scratch database runs in its own child, so nothing a
run leaves behind in the process (module-level caches of historical models,
mutated field objects, registered signal handlers, monkeypatches...) can
affect the next run or the analysis. The child sends its messages and its
result back over a pipe (pickled); the parent keeps creating and dropping
the scratch databases, so a crashed or killed child can't leave one behind.

Without os.fork() (Windows), runs happen in-process, with a warning.
"""

from __future__ import annotations

import os
import pickle
import signal
import sys
import traceback
import warnings
from collections.abc import Callable
from typing import Any, TypeVar

from django_clean_migrations.scratch import close_everything

T = TypeVar("T")

Send = Callable[[Any], None]

CAN_FORK = hasattr(os, "fork")


class ChildError(Exception):
    """An exception in the child that couldn't be sent back as itself."""

    def __init__(self, message: str, child_traceback: str = "") -> None:
        super().__init__(message)
        self.child_traceback = child_traceback

    def __reduce__(self) -> Any:
        return (ChildError, (str(self), self.child_traceback))


def run_isolated(
    function: Callable[[Send], T],
    on_message: Callable[[Any], None] | None = None,
    *,
    fork: bool = True,
) -> T:
    """
    Call ``function(send)`` in a forked child and return its result;
    ``send(message)`` in the child calls ``on_message(message)`` here.
    Exceptions are re-raised here: as themselves if they can be pickled,
    else as ChildError.
    """
    receive = on_message or (lambda message: None)
    if not (fork and CAN_FORK):
        if fork:
            warnings.warn(
                "os.fork() isn't available: running migrations in-process, so"
                " state they leave in the process can leak between runs.",
                RuntimeWarning,
                stacklevel=2,
            )
        return function(receive)

    close_everything()
    for stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
        try:
            if stream is not None:
                stream.flush()
        except Exception:
            pass
    read_fd, write_fd = os.pipe()
    with warnings.catch_warnings():
        # Python 3.12+ warns about forking with threads running (such as a
        # progress spinner): the child only touches the pipe and the
        # database, not what those threads guard.
        warnings.simplefilter("ignore", DeprecationWarning)
        pid = os.fork()
    in_child = pid == 0
    # Coverage doesn't trace the forking frame in the child.
    if in_child:  # pragma: no cover - in the child, untraced
        _child(function, read_fd, write_fd)
    os.close(write_fd)
    result: list[Any] = []
    failure: list[BaseException] = []
    try:
        with os.fdopen(read_fd, "rb") as pipe:
            while True:
                try:
                    kind, payload = pickle.load(pipe)
                except EOFError:
                    break
                if kind == "message":
                    receive(payload)
                elif kind == "result":
                    result.append(payload)
                else:
                    failure.append(payload)
    except BaseException:
        _kill(pid)
        raise
    _, status = os.waitpid(pid, 0)
    if failure:
        raise failure[0]
    if not result:
        raise ChildError(
            "The migrate run's child process ended without a result"
            f" ({_describe_status(status)})."
        )
    return result[0]  # type: ignore[no-any-return]


def _child(function: Callable[[Send], Any], read_fd: int, write_fd: int) -> None:
    os.close(read_fd)
    # A progress display may have replaced stdout/stderr with proxies that
    # take locks held by threads that don't exist here.
    sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
    signal.signal(signal.SIGINT, signal.default_int_handler)
    code = 0
    try:
        with os.fdopen(write_fd, "wb") as pipe:

            def send(kind: str, payload: Any) -> None:
                pickle.dump((kind, payload), pipe)
                pipe.flush()

            try:
                value = function(lambda message: send("message", message))
                send("result", value)
            except BaseException as exc:
                code = 1
                try:
                    pickle.loads(pickle.dumps(exc))
                    send("error", exc)
                except Exception:
                    send(
                        "error",
                        ChildError(
                            f"{type(exc).__name__}: {exc}", traceback.format_exc()
                        ),
                    )
            try:
                close_everything()
            except Exception:
                pass
    finally:
        os._exit(code)


def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def _describe_status(status: int) -> str:
    if os.WIFSIGNALED(status):
        return f"killed by signal {os.WTERMSIG(status)}"
    return f"exit status {os.WEXITSTATUS(status)}"
