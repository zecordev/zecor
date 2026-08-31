# SPDX-License-Identifier: Apache-2.0
"""retrieval: symbol index + allow_files suggestion (build-queue item E)."""

from __future__ import annotations

from pathlib import Path

import pytest

from zecor import config, retrieval


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "ZECOR_ROOT", tmp_path / "state")
    r = tmp_path / "repo"
    (r / "auth").mkdir(parents=True)
    (r / "auth" / "login.py").write_text(
        '"""Login flow."""\n\ndef sign_in(user):\n    return True\n\n'
        "class SessionToken:\n    pass\n"
    )
    (r / "auth" / "reset.py").write_text("def send_reset_email(addr):\n    return addr\n")
    (r / "billing.py").write_text("def charge_card(amount):\n    return amount\n")
    (r / "node_modules").mkdir()
    (r / "node_modules" / "junk.py").write_text("def sign_in():\n    pass\n")  # must be skipped
    return r


def test_symbol_index_maps_names_to_files_and_skips_vendor(repo: Path) -> None:
    idx = retrieval.symbol_index(repo)
    assert idx["sign_in"] == ["auth/login.py"]
    assert idx["SessionToken"] == ["auth/login.py"]
    assert idx["charge_card"] == ["billing.py"]
    assert all("node_modules" not in p for ps in idx.values() for p in ps)


def test_symbol_index_is_cached(repo: Path) -> None:
    retrieval.symbol_index(repo)
    caches = list((config.ZECOR_ROOT / ".cache").glob("symbols-*.json"))
    assert len(caches) == 1
    # a second call must not re-parse -- corrupt a source file, index stays the old one
    (repo / "billing.py").write_text("def totally_different():\n    pass\n")
    idx = retrieval.symbol_index(repo)
    assert "charge_card" in idx  # served from cache (mtime count unchanged in the same sec)


def test_lexical_files_resolves_identifiers_from_the_task_text(repo: Path) -> None:
    assert retrieval.lexical_files(repo, "the sign_in helper drops the SessionToken") == [
        "auth/login.py"
    ]
    assert retrieval.lexical_files(repo, "fix charge_card rounding") == ["billing.py"]
    assert retrieval.lexical_files(repo, "nothing recognisable here") == []


def test_names_in_splits_camel_and_snake() -> None:
    got = set(retrieval._names_in("the sendResetEmail path and reset_token flow"))
    assert {"send", "Reset", "Email", "reset", "token", "flow"} <= got


def test_suggest_falls_back_to_lexical_when_embeddings_are_down(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zecor.localmodel as lm

    def boom(*a: object, **k: object) -> object:
        raise lm.LocalModelError("no embed server")

    monkeypatch.setattr(lm, "embed", boom)
    # one lexical hit + k=3 -> embed_rank is asked to fill 2 and fails -> lexical stands,
    # padded with the deterministic file order
    out = retrieval.suggest(repo, "sign_in tweak", k=3)
    assert out[0] == "auth/login.py"
    assert len(out) <= 3 and len(set(out)) == len(out)


def test_embed_rank_orders_by_cosine(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import zecor.localmodel as lm

    # fake embeddings: query close to billing.py, far from the rest
    table = {
        "q": [1.0, 0.0],
        "billing.py": [0.9, 0.1],
        "auth/login.py": [0.0, 1.0],
        "auth/reset.py": [-1.0, 0.0],
    }

    def fake_embed(texts: list[str], **k: object) -> list[list[float]]:
        out = [table["q"]]
        for blob in texts[1:]:
            path = blob.split("\n", 1)[0]
            out.append(table.get(path, [0.0, 0.0]))
        return out

    monkeypatch.setattr(lm, "embed", fake_embed)
    ranked = retrieval.embed_rank(
        repo, "charge things", ["auth/login.py", "billing.py", "auth/reset.py"], k=2
    )
    assert ranked[0] == "billing.py"