# SPDX-License-Identifier: Apache-2.0
"""Serialized card writes -- Command Center build step 1.

Every mutation of a `tasks/<id>.md` file goes through `mutate()`: an flock on
`tasks/.lock` held across the whole read-modify-write, so two writers can never
interleave into a lost update, and the write itself is tmp-file + `os.replace` so a
reader never sees a half-written card. A `rev:` counter bumps per content write; a
caller wanting strict optimistic concurrency passes it back as `base_rev` and is
refused with `StaleRev` if it moved -- `cmd_retry` instead re-checks its precondition
*inside* `fn`, a proper CAS since `fn` runs under the lock. Dispatch leases live in
SQLite, not here (see `zecor.core.leases`); this module holds no DB handle.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

from zecor import config
from zecor.cards import Card
from zecor.core.locks import file_lock


class CardNotFound(Exception):
    pass


class StaleRev(Exception):
    """A mutate() whose base rev no longer matches the card on disk -- the caller's copy
    is behind a concurrent write and must re-read before trying again."""


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    with file_lock(config.TASKS_DIR / ".lock"):
        yield


def _card_path(card_id: str) -> Path:
    return config.TASKS_DIR / f"{card_id}.md"


def _read_unlocked(card_id: str) -> Card:
    path = _card_path(card_id)
    try:
        return Card.parse(path.read_text())
    except FileNotFoundError as e:
        raise CardNotFound(card_id) from e


def _atomic_write(card: Card) -> None:
    path = _card_path(card.id)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{card.id}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(card.render())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read(card_id: str) -> Card:
    """Snapshot a card. Cheap and lock-free -- use `mutate` to change it."""
    with _locked():
        return _read_unlocked(card_id)


class CardExists(Exception):
    pass


def next_id() -> int:
    """The next free 4-digit card id (max existing + 1, or 1)."""
    try:
        nums = [
            int(p.stem)
            for p in config.TASKS_DIR.glob("[0-9]*.md")
            if p.stem.isdigit()
        ]
    except OSError:
        nums = []
    return (max(nums) + 1) if nums else 1


def create(card: Card) -> Path:
    """Write a brand-new card. Refuses to overwrite an existing id. Atomic, under the
    lock -- the one sanctioned way for `zecor plan` to land a card file."""
    with _locked():
        path = _card_path(card.id)
        if path.exists():
            raise CardExists(card.id)
        _atomic_write(card)
        return path


def mutate(
    card_id: str,
    fn: Callable[[Card], None],
    *,
    base_rev: int | None = None,
    bump_rev: bool = True,
) -> Card:
    """Apply `fn` to the card under the lock and persist it; `fn` mutates the Card in
    place, its return value ignored. Raises `StaleRev` and writes nothing if `base_rev`
    is given and the on-disk rev has moved past it. `bump_rev=False` is for an
    ephemeral touch (a lease heartbeat) that must not invalidate a concurrent editor's
    content view."""
    with _locked():
        card = _read_unlocked(card_id)
        if base_rev is not None and card.rev != base_rev:
            raise StaleRev(f"{card_id}: base rev {base_rev}, on disk {card.rev}")
        fn(card)
        if bump_rev:
            card.rev += 1
        _atomic_write(card)
        return card