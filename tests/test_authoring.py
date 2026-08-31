# SPDX-License-Identifier: Apache-2.0
"""Write the failing test first (feature asks)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.fakes import make_repo
from zecor.authoring import author_failing_test, is_test_path, wants_test_first
from zecor.cards import Card
from zecor.task import Klass
from zecor.workers.base import VerifyRun, WorkerResult


def _card(intent: str, klass: Klass = Klass.BOUNDED) -> Card:
    return Card("0001", "demo", intent, "./.fleet/verify.sh", ["kvstore/store.py"], klass=klass)


def test_wants_test_first_only_for_feature_asks() -> None:
    assert wants_test_first(_card("add a health endpoint")) is True
    assert wants_test_first(_card("implement retry with backoff")) is True
    assert wants_test_first(_card("fix the failing health test")) is False  # bug language
    assert wants_test_first(_card("add handling for the crash on empty input")) is False
    assert wants_test_first(_card("add a flag", klass=Klass.UNBOUNDED)) is False
    assert wants_test_first(_card("rename the module")) is False  # no feature verb


def test_is_test_path() -> None:
    assert is_test_path("tests/test_x.py")
    assert is_test_path("src/foo_test.py")
    assert is_test_path("web/Button.spec.tsx")
    assert not is_test_path("src/foo.py")


class _Worker:
    def __init__(self, writes: dict[str, str], outcome: str = "pass") -> None:
        self.writes = writes
        self.outcome = outcome

    def __call__(self, route, job):
        for rel, body in self.writes.items():
            p = job.lane_dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
        return WorkerResult(self.outcome, verify=None)


def _lane(tmp_path: Path, gate: str):
    from zecor.lanes import open_lane

    repo = make_repo(tmp_path / "repo", verify_body=gate)
    return open_lane("0001", repo)


def _verify(job) -> VerifyRun:
    rc = subprocess.run(
        ["/bin/sh", "-c", job.verify_cmd], cwd=job.lane_dir, capture_output=True, text=True
    ).returncode
    return VerifyRun(rc, "", "")


def test_author_ok_when_a_new_test_makes_the_gate_red(tmp_path: Path) -> None:
    lane = _lane(tmp_path, gate="grep -rq FEATURE tests/ && exit 1 || exit 0")
    w = _Worker({"tests/test_feature.py": "# FEATURE: not built yet\n"})
    res = author_failing_test(_card("add the feature"), lane, dispatcher=w, verifier=_verify)
    assert res.ok
    assert res.verify is not None and not res.verify.ok


def test_author_fails_if_gate_stays_green(tmp_path: Path) -> None:
    lane = _lane(tmp_path, gate="exit 0")
    w = _Worker({"tests/test_feature.py": "# a test that does not fail\n"})
    res = author_failing_test(_card("add the feature"), lane, dispatcher=w, verifier=_verify)
    assert not res.ok and "does not fail" in res.note


def test_author_reverts_an_out_of_scope_edit_but_keeps_a_real_failing_test(
    tmp_path: Path,
) -> None:
    # the model "helps" by implementing (src.txt) AND writes a test that asserts it.
    # the impl is reverted; the test now fails on its own -> salvaged.
    lane = _lane(tmp_path, gate="grep -q DONE src.txt")
    w = _Worker({"src.txt": "// DONE\n", "tests/test_x.py": "# asserts DONE behaviour\n"})
    res = author_failing_test(_card("add the feature"), lane, dispatcher=w, verifier=_verify)
    assert res.ok
    assert "reverted" in res.note
    assert (lane.path / "src.txt").read_text() == "// v0\n"  # impl undone
    assert (lane.path / "tests" / "test_x.py").exists()  # test kept


def test_author_fails_when_only_the_impl_was_written(tmp_path: Path) -> None:
    lane = _lane(tmp_path, gate="grep -q DONE src.txt")
    w = _Worker({"src.txt": "// DONE\n"})  # no test at all
    res = author_failing_test(_card("add the feature"), lane, dispatcher=w, verifier=_verify)
    assert not res.ok and "wrote no test" in res.note
    assert (lane.path / "src.txt").read_text() == "// v0\n"  # reverted


def test_author_rejects_a_dispatch_nonresult(tmp_path: Path) -> None:
    lane = _lane(tmp_path, gate="exit 1")
    w = _Worker({}, outcome="timeout")
    res = author_failing_test(_card("add the feature"), lane, dispatcher=w, verifier=_verify)
    assert not res.ok and "timeout" in res.note