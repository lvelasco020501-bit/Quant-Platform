"""The lock that makes "exactly one paper session" checkable rather than assumed.

Two live sessions ran against one state directory for eighteen hours and nothing anywhere
could contradict either of them. These tests hold the lock to the two properties that
would have stopped it: a second session is refused while the first is alive, and a lock
left behind by a dead process never becomes a permanent blockade.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

import pytest

from quantplatform.core.errors import ConfigurationError, StorageError
from quantplatform.storage.session_lock import (
    LOCK_FILENAME,
    SessionLock,
    SessionLockRecord,
    read_session_lock,
)
from tests.factories import ANCHOR

_SESSION = "paper-7c-week3"
_OTHER_SESSION = "paper-7c-soak-test"

# A pid that cannot be running: the kernel reserves 0 for the scheduler and never assigns
# it to a user process, so os.kill(0, 0) does not mean "this process". Using a large
# arbitrary number instead would eventually collide with a real process on a busy machine
# and make this suite flaky for reasons having nothing to do with locking.
_DEAD_PID = 999_999_999


def _lock(directory: Path, session_id: str = _SESSION, pid: int | None = None) -> SessionLock:
    return SessionLock(directory=directory, session_id=session_id, pid=pid)


# --- Claiming -------------------------------------------------------------------------------


def test_acquiring_writes_a_lock_naming_this_session_and_process(tmp_path: Path) -> None:
    lock = _lock(tmp_path)

    lock.acquire(now=ANCHOR)

    record = read_session_lock(tmp_path)
    assert record is not None
    assert record.session_id == _SESSION
    assert record.pid == os.getpid()
    assert record.started_at == ANCHOR
    assert lock.is_held is True


def test_the_lock_lands_beside_the_state_it_governs(tmp_path: Path) -> None:
    # Co-location is the point: the directory an operator already looks in for the
    # session is where they must find the answer to "what is running".
    lock = _lock(tmp_path)

    assert lock.path == tmp_path / LOCK_FILENAME


def test_the_payload_is_plain_json_a_foreign_reader_can_parse(tmp_path: Path) -> None:
    # The dashboard lives on another branch and reads this file without importing the
    # platform. Anything richer than three plain fields would break that.
    _lock(tmp_path).acquire(now=ANCHOR)

    payload = json.loads((tmp_path / LOCK_FILENAME).read_text(encoding="utf-8"))

    assert set(payload) == {"session_id", "pid", "started_at"}
    assert payload["session_id"] == _SESSION
    assert isinstance(payload["pid"], int)


def test_the_directory_is_created_if_it_does_not_exist(tmp_path: Path) -> None:
    directory = tmp_path / "state"

    _lock(directory).acquire(now=ANCHOR)

    assert (directory / LOCK_FILENAME).is_file()


# --- Refusing a second session --------------------------------------------------------------


def test_a_second_session_is_refused_while_the_first_is_alive(tmp_path: Path) -> None:
    # The eighteen-hour incident, in one assertion. The holder's pid is this test's own
    # process, which is unambiguously alive.
    _lock(tmp_path, session_id=_OTHER_SESSION).acquire(now=ANCHOR)

    with pytest.raises(ConfigurationError, match="already running") as caught:
        _lock(tmp_path, session_id=_SESSION).acquire(now=ANCHOR)

    details = caught.value.details
    assert details["running_session_id"] == _OTHER_SESSION
    assert details["running_pid"] == os.getpid()
    assert details["requested_session_id"] == _SESSION


def test_even_the_same_session_id_is_refused_twice(tmp_path: Path) -> None:
    # Not a de-duplication check on the name — a check on the *directory*. Two processes
    # sharing a state directory collide whether or not they call themselves the same thing.
    _lock(tmp_path).acquire(now=ANCHOR)

    with pytest.raises(ConfigurationError, match="already running"):
        _lock(tmp_path).acquire(now=ANCHOR)


def test_the_refusal_names_the_lock_file_so_an_operator_can_inspect_it(tmp_path: Path) -> None:
    _lock(tmp_path).acquire(now=ANCHOR)

    with pytest.raises(ConfigurationError) as caught:
        _lock(tmp_path, session_id=_SESSION).acquire(now=ANCHOR)

    assert caught.value.details["lock_file"] == str(tmp_path / LOCK_FILENAME)


def test_a_refused_claim_does_not_overwrite_the_holder(tmp_path: Path) -> None:
    _lock(tmp_path, session_id=_OTHER_SESSION).acquire(now=ANCHOR)

    with pytest.raises(ConfigurationError):
        _lock(tmp_path, session_id=_SESSION).acquire(now=ANCHOR + timedelta(hours=1))

    record = read_session_lock(tmp_path)
    assert record is not None
    assert record.session_id == _OTHER_SESSION


# --- Stale locks: a crash must never block a restart ----------------------------------------


def test_a_lock_whose_process_is_gone_is_reclaimed(tmp_path: Path) -> None:
    # The failure mode that would be worse than the one this module prevents: an operator
    # locked out by a file a killed process left behind.
    _lock(tmp_path, session_id=_OTHER_SESSION, pid=_DEAD_PID).acquire(now=ANCHOR)

    _lock(tmp_path, session_id=_SESSION).acquire(now=ANCHOR + timedelta(hours=1))

    record = read_session_lock(tmp_path)
    assert record is not None
    assert record.session_id == _SESSION
    assert record.pid == os.getpid()


def test_an_unreadable_lock_does_not_block_a_claim(tmp_path: Path) -> None:
    # A torn or corrupt lock reads as no lock. Refusing to start on an unparseable file
    # would strand an operator with no path forward but deleting a file by hand.
    (tmp_path / LOCK_FILENAME).write_text("{ this is not json", encoding="utf-8")

    _lock(tmp_path).acquire(now=ANCHOR)

    record = read_session_lock(tmp_path)
    assert record is not None
    assert record.session_id == _SESSION


@pytest.mark.parametrize(
    "payload",
    [
        '{"session_id": "x"}',
        '{"pid": 1}',
        '{"session_id": "x", "pid": "not-a-number", "started_at": "2026-01-01T00:00:00+00:00"}',
        '{"session_id": "x", "pid": 1, "started_at": "not-a-time"}',
        "[]",
    ],
)
def test_a_malformed_lock_reads_as_absent(tmp_path: Path, payload: str) -> None:
    (tmp_path / LOCK_FILENAME).write_text(payload, encoding="utf-8")

    assert read_session_lock(tmp_path) is None


def test_no_lock_file_reads_as_absent(tmp_path: Path) -> None:
    assert read_session_lock(tmp_path) is None


# --- Releasing ------------------------------------------------------------------------------


def test_releasing_removes_the_lock(tmp_path: Path) -> None:
    lock = _lock(tmp_path)
    lock.acquire(now=ANCHOR)

    lock.release()

    assert read_session_lock(tmp_path) is None
    assert lock.is_held is False


def test_releasing_is_idempotent(tmp_path: Path) -> None:
    # A shutdown path that cannot run twice is a shutdown path that fails during shutdown.
    lock = _lock(tmp_path)
    lock.acquire(now=ANCHOR)

    lock.release()
    lock.release()

    assert read_session_lock(tmp_path) is None


def test_releasing_a_lock_never_held_does_nothing(tmp_path: Path) -> None:
    # Startup can fail before the claim; releasing nothing is the correct response.
    _lock(tmp_path, session_id=_OTHER_SESSION).acquire(now=ANCHOR)

    _lock(tmp_path).release()

    record = read_session_lock(tmp_path)
    assert record is not None
    assert record.session_id == _OTHER_SESSION


def test_a_released_lock_lets_the_next_session_start(tmp_path: Path) -> None:
    first = _lock(tmp_path, session_id=_OTHER_SESSION)
    first.acquire(now=ANCHOR)
    first.release()

    _lock(tmp_path, session_id=_SESSION).acquire(now=ANCHOR + timedelta(minutes=1))

    record = read_session_lock(tmp_path)
    assert record is not None
    assert record.session_id == _SESSION


# --- Liveness -------------------------------------------------------------------------------


def test_a_record_naming_this_process_is_alive() -> None:
    record = SessionLockRecord(session_id=_SESSION, pid=os.getpid(), started_at=ANCHOR)

    assert record.is_alive is True


def test_a_record_naming_a_dead_process_is_not_alive() -> None:
    record = SessionLockRecord(session_id=_SESSION, pid=_DEAD_PID, started_at=ANCHOR)

    assert record.is_alive is False


# --- Failure surfaces -----------------------------------------------------------------------


def test_an_unwritable_directory_raises_rather_than_starting_unlocked(tmp_path: Path) -> None:
    # Failing closed: if the lock cannot be written, the session must not run believing it
    # holds one. Starting unlocked is the exact condition that produced two live sessions.
    blocker = tmp_path / "blocked"
    blocker.write_text("", encoding="utf-8")

    with pytest.raises(StorageError, match="could not be written"):
        _lock(blocker / "state").acquire(now=ANCHOR)


def test_acquiring_requires_an_explicitly_injected_clock(tmp_path: Path) -> None:
    # Every other component here takes an injected clock, and this one is no exception:
    # `now` is a required keyword, so no caller can acquire a lock without stating which
    # clock stamped it. That is why there is no `with` block — a context manager would
    # have to read a wall clock behind the caller's back to fill this in.
    with pytest.raises(TypeError, match="now"):
        _lock(tmp_path).acquire()  # type: ignore[call-arg]
