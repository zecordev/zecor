# SPDX-License-Identifier: Apache-2.0
"""core.cards: the serialized card writer + rev CAS (CC build step 1).

Dispatch leases moved to zecor.core.leases (SQLite) -- see test_core_leases.py.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from zecor import config
from zecor.cards import Card
from zecor.core import cards as cc


@pytest.fixture
def tasks_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "tasks"
    d.mkdir()
    monkeypatch.setattr(config, "TASKS_DIR", d)
    return d


def _mk(tasks_dir: Path, cid: str = "0001", **kw: object) -> Card:
    card = Card(cid, "demo", "do a thing", "./.fleet/verify.sh", ["a.py"], **kw)  # type: ignore[arg-type]
    card.write(tasks_dir)
    return card


def test_round_trip_preserves_unknown_frontmatter_keys(tasks_dir: Path) -> None:
    path = tasks_dir / "0001.md"
    path.write_text(
        "---\nid: 0001\nrepo: demo\nklass: bounded\nstate: ready\n"
        "verify_cmd: ./x\nfuture_field: keep me\nweird_key: 7\n"
        "allow_files:\n  - a.py\nnon_goals:\n---\n\n# do a thing\n"
    )
    card = cc.read("0001")
    assert card.extra == {"future_field": "keep me", "weird_key": "7"}
    cc.mutate("0001", lambda c: setattr(c, "state", "landed"))
    text2 = path.read_text()
    assert "future_field: keep me" in text2
    assert "weird_key: 7" in text2
    assert cc.read("0001").state == "landed"


def test_mutate_bumps_rev_and_rejects_a_stale_base(tasks_dir: Path) -> None:
    _mk(tasks_dir)
    assert cc.read("0001").rev == 0
    cc.mutate("0001", lambda c: setattr(c, "tag", "hard"))
    assert cc.read("0001").rev == 1
    with pytest.raises(cc.StaleRev):
        cc.mutate("0001", lambda c: setattr(c, "tag", "x"), base_rev=0)
    cc.mutate("0001", lambda c: setattr(c, "tag", "ok"), base_rev=1)
    assert cc.read("0001").tag == "ok"


def test_missing_card_raises(tasks_dir: Path) -> None:
    with pytest.raises(cc.CardNotFound):
        cc.read("9999")
    with pytest.raises(cc.CardNotFound):
        cc.mutate("9999", lambda c: None)


def test_two_hundred_concurrent_mutations_lose_nothing(tasks_dir: Path) -> None:
    _mk(tasks_dir)

    def bump(_: int) -> None:
        def _fn(c: Card) -> None:
            c.retry_count += 1

        cc.mutate("0001", _fn)

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(bump, range(200)))

    card = cc.read("0001")
    assert card.retry_count == 200
    assert card.rev == 200