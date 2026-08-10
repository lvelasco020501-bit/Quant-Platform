"""Phase 7C.0: the durable state repository a multi-day session depends on.

Everything below is about one question: after a restart, is the account the same account?
A repository that answers "nearly" is worse than one that answers "no", because a session
resumed onto slightly wrong balances keeps trading and looks fine.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel

from quantplatform.core.enums import ExecutionMode
from quantplatform.core.errors import StorageError
from quantplatform.core.interfaces import PaperStateRepository
from quantplatform.core.models.market import MarketBar
from quantplatform.core.models.paper import PaperSessionState
from quantplatform.core.models.portfolio import Balance, Position
from quantplatform.core.models.telemetry import FeedMetricsSnapshot
from quantplatform.storage.paper_state import FilePaperStateRepository
from tests.factories import ANCHOR, make_balance, make_bar, make_position


def _state(**overrides: object) -> PaperSessionState:
    defaults: dict[str, object] = {
        "session_id": "session-1",
        "strategy_id": "buy_then_sell",
        "execution_mode": ExecutionMode.PAPER,
        "quote_asset": "USDT",
        "started_at": ANCHOR,
        "saved_at": ANCHOR + timedelta(hours=6),
        "balances": (
            make_balance(asset="USDT", free=Decimal("9750.25"), locked=Decimal("100.75")),
            make_balance(asset="BTC", free=Decimal("0.00312")),
        ),
        "last_bar": make_bar(index=5),
        "bars_processed": 6,
        "realized_pnl": Decimal("-12.5"),
        "total_fees": Decimal("3.75"),
        "restarts": 2,
        "feed_baseline": FeedMetricsSnapshot(
            reconnect_count=3,
            detected_gaps=1,
            candles_received=90,
            candles_accepted=88,
            candles_rejected=2,
            rejected_frames=2,
        ),
    }
    return PaperSessionState(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_the_file_repository_satisfies_the_port(tmp_path: Path) -> None:
    assert isinstance(FilePaperStateRepository(tmp_path), PaperStateRepository)


def test_a_session_that_was_never_saved_loads_as_nothing(tmp_path: Path) -> None:
    assert FilePaperStateRepository(tmp_path).load("absent") is None


def test_everything_a_restart_needs_survives_the_round_trip(tmp_path: Path) -> None:
    # The five things Phase 7C.0 requires be restorable, checked one by one rather than by
    # equality alone, so a failure names which one was lost.
    repository = FilePaperStateRepository(tmp_path)
    original = _state()

    repository.save(original)
    restored = repository.load("session-1")

    assert restored is not None
    assert restored == original
    assert restored.session_id == "session-1"
    assert restored.balances == original.balances
    assert restored.feed_baseline == original.feed_baseline
    assert restored.last_bar == original.last_bar
    assert restored.bars_processed == 6
    assert restored.restarts == 2


def test_decimals_survive_exactly(tmp_path: Path) -> None:
    # Money read back as a float would be a different account.
    repository = FilePaperStateRepository(tmp_path)
    repository.save(_state())

    restored = repository.load("session-1")

    assert restored is not None
    assert restored.balances[0].free == Decimal("9750.25")
    assert restored.balances[1].free == Decimal("0.00312")
    assert restored.realized_pnl == Decimal("-12.5")


def test_derived_fields_are_not_stored_but_are_recomputed(tmp_path: Path) -> None:
    # `total` and `cost_basis` are computed: writing them would make the document
    # unreadable, and omitting them costs nothing because they are functions of what is.
    repository = FilePaperStateRepository(tmp_path)
    original = _state(positions=(make_position(quantity=Decimal("0.25")),))
    repository.save(original)

    document = json.loads(repository.path_for("session-1").read_text(encoding="utf-8"))
    assert "total" not in document["balances"][0]
    assert "cost_basis" not in document["positions"][0]

    restored = repository.load("session-1")
    assert restored is not None
    assert restored.balances[0].total == original.balances[0].total
    assert restored.positions[0].cost_basis == original.positions[0].cost_basis


def test_the_exclusion_map_covers_every_computed_field() -> None:
    # A drift guard. If a new computed field is added anywhere in the state tree, this
    # fails and points at the adapter's exclusion map, rather than a restart failing in
    # production on day four.
    def computed_in(model: type[BaseModel]) -> set[str]:
        return set(model.model_computed_fields)

    assert computed_in(Balance) == {"total"}
    assert computed_in(Position) == {"cost_basis"}
    assert computed_in(MarketBar) == set()
    assert computed_in(PaperSessionState) == set()
    assert computed_in(FeedMetricsSnapshot) == set()


def test_saving_twice_replaces_rather_than_appends(tmp_path: Path) -> None:
    repository = FilePaperStateRepository(tmp_path)
    repository.save(_state())
    repository.save(_state(bars_processed=99, last_bar=make_bar(index=98)))

    restored = repository.load("session-1")

    assert restored is not None
    assert restored.bars_processed == 99
    assert repository.session_ids() == ("session-1",)


def test_no_temporary_file_is_left_behind(tmp_path: Path) -> None:
    # A stray temp file would be picked up by session_ids() and look like a second session.
    repository = FilePaperStateRepository(tmp_path)
    repository.save(_state())

    assert [path.name for path in sorted(tmp_path.iterdir())] == ["session-1.json"]


def test_a_partially_written_document_never_replaces_a_good_one(tmp_path: Path) -> None:
    # The crash-safety claim. A temp file left by a killed process must not be mistaken for
    # the real snapshot, and the previous complete snapshot must still load.
    repository = FilePaperStateRepository(tmp_path)
    repository.save(_state())
    orphan = tmp_path / "session-1.json.tmp"
    orphan.write_text('{"session_id": "session-1", "trunca', encoding="utf-8")

    restored = repository.load("session-1")

    assert restored is not None
    assert restored.bars_processed == 6
    assert repository.session_ids() == ("session-1",)


def test_a_corrupt_document_is_refused_rather_than_ignored(tmp_path: Path) -> None:
    # Starting fresh instead would silently discard the account that existed.
    repository = FilePaperStateRepository(tmp_path)
    repository.path_for("session-1").write_text('{"session_id": "x"}', encoding="utf-8")

    with pytest.raises(StorageError, match="not valid"):
        repository.load("session-1")


def test_unreadable_json_is_refused(tmp_path: Path) -> None:
    repository = FilePaperStateRepository(tmp_path)
    repository.path_for("session-1").write_text("not json at all", encoding="utf-8")

    with pytest.raises(StorageError, match="not valid"):
        repository.load("session-1")


def test_deleting_removes_the_document_and_is_safe_when_absent(tmp_path: Path) -> None:
    repository = FilePaperStateRepository(tmp_path)
    repository.save(_state())

    repository.delete("session-1")
    repository.delete("session-1")

    assert repository.load("session-1") is None
    assert repository.session_ids() == ()


def test_sessions_do_not_share_a_document(tmp_path: Path) -> None:
    repository = FilePaperStateRepository(tmp_path)
    repository.save(_state(session_id="alpha"))
    repository.save(_state(session_id="beta", bars_processed=42, last_bar=make_bar(index=41)))

    assert repository.session_ids() == ("alpha", "beta")
    alpha = repository.load("alpha")
    assert alpha is not None
    assert alpha.bars_processed == 6


@pytest.mark.parametrize("session_id", ["../escape", "nested/session", "", ".", ".."])
def test_a_session_id_that_is_not_a_file_name_is_refused(tmp_path: Path, session_id: str) -> None:
    # Refused rather than sanitised: silently renaming an operator's session would let two
    # sessions collide under one document.
    with pytest.raises(StorageError, match="not usable as a file name"):
        FilePaperStateRepository(tmp_path).path_for(session_id)


def test_the_repository_creates_its_directory(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "state"

    repository = FilePaperStateRepository(target)

    assert target.is_dir()
    assert repository.directory == target


def test_an_unusable_directory_is_refused_at_construction(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")

    with pytest.raises(StorageError, match="could not be created"):
        FilePaperStateRepository(blocker / "state")
