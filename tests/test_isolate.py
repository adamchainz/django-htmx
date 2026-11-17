from __future__ import annotations

import os
import pickle
import signal
import sys
import warnings
from typing import Any

import pytest

from django_compact_migrations import isolate
from django_compact_migrations.isolate import ChildError, run_isolated
from django_compact_migrations.runner import RunError
from tests.project import Base


class IsolateTests(Base):
    def test_run_isolated(self):
        parent = os.getpid()
        messages: list[Any] = []

        def child(send: Any) -> int:
            send("hello")
            return os.getpid()

        assert run_isolated(child, messages.append) != parent
        assert messages == ["hello"]

        def failing(send: Any) -> None:
            raise RunError("boom", ("shop", "0001_initial"))

        with pytest.raises(RunError, match="boom") as info:
            run_isolated(failing)
        assert info.value.migration == ("shop", "0001_initial")

        def crashing(send: Any) -> None:
            os._exit(3)

        with pytest.raises(ChildError, match="exit status 3"):
            run_isolated(crashing)

    def test_child_error_pickles(self):
        error = pickle.loads(pickle.dumps(ChildError("boom", "trace")))
        assert str(error) == "boom" and error.child_traceback == "trace"

    def test_without_fork(self):
        self.monkeypatch.setattr(isolate, "CAN_FORK", False)
        messages: list[Any] = []

        def function(send: Any) -> int:
            send("hi")
            return os.getpid()

        with pytest.warns(RuntimeWarning, match="os.fork"):
            assert run_isolated(function, messages.append) == os.getpid()
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert run_isolated(function, messages.append, fork=False) == os.getpid()
        assert messages == ["hi", "hi"]

    def test_unpicklable_error_in_child(self):
        class Odd(Exception):
            def __init__(self) -> None:
                super().__init__("odd")
                self.callback = lambda: None

        def function(send: Any) -> None:
            raise Odd()

        with pytest.raises(ChildError, match="Odd: odd") as info:
            run_isolated(function)
        assert "Traceback" in info.value.child_traceback

    def test_cleanup_failure_in_child_ignored(self):
        parent = os.getpid()

        def close() -> None:
            if os.getpid() != parent:
                raise RuntimeError("can't close")

        self.monkeypatch.setattr(isolate, "close_everything", close)
        assert run_isolated(lambda send: 42) == 42

    def test_child_killed(self):
        def function(send: Any) -> None:  # pragma: no cover - its child is killed
            os.kill(os.getpid(), signal.SIGKILL)

        with pytest.raises(ChildError, match="killed by signal 9"):
            run_isolated(function)

    def test_parent_interrupted_kills_child(self):
        def function(send: Any) -> None:  # pragma: no cover - its child is killed
            send("started")
            signal.pause()

        def receive(message: Any) -> None:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            run_isolated(function, receive)

    def test_missing_or_broken_stream(self):
        class Broken:
            def flush(self) -> None:
                raise ValueError("closed")

        self.monkeypatch.setattr(sys, "__stderr__", None)
        self.monkeypatch.setattr(sys, "stdout", Broken())
        assert run_isolated(lambda send: 1) == 1

    def test_kill_gone_child(self):
        pid = os.fork()
        if pid == 0:  # pragma: no cover - the child exits at once
            os._exit(0)
        os.waitpid(pid, 0)
        isolate._kill(pid)  # neither there to kill nor to wait for
