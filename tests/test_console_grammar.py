# SPDX-License-Identifier: Apache-2.0
"""console.grammar: Layer-0 command parsing (Command Center build step 5)."""

from __future__ import annotations

import pytest

from zecor.console import grammar
from zecor.console.grammar import Parsed, Unknown


@pytest.mark.parametrize(
    ("text", "verb", "args"),
    [
        ("stop", "stop", {}),
        ("please halt everything now", "stop", {}),
        ("go", "go", {}),
        ("resume the fleet", "go", {}),
        ("run", "run", {}),
        ("run until 07:00", "run", {"until": "07:00"}),
        ("run max 5", "run", {"max": "5"}),
        ("kick off a run until 23:30 max 10", "run", {"until": "23:30", "max": "10"}),
        ("plan", "plan", {}),
        ("retry 0042", "retry", {"id": "0042"}),
        ("retry 42 with opus", "retry", {"id": "42", "model": "opus"}),
        ("please retry card 0042 using sonnet", "retry", {"id": "0042", "model": "sonnet"}),
        ("retry 7 on opus tier t4 note use a set literal", "retry",
         {"id": "7", "model": "opus", "tier": "t4", "note": "use a set literal"}),
        ("assign 12 to console", "assign", {"id": "12", "move": "console"}),
        ("move card 0007 under deep-review", "assign", {"id": "0007", "move": "deep-review"}),
        ("put 3 cc-tui", "assign", {"id": "3", "move": "cc-tui"}),
        ("clear 0009", "clear_zombie", {"id": "0009"}),
        ("un-zombie 12", "clear_zombie", {"id": "12"}),
        ("kill card 3", "clear_zombie", {"id": "3"}),
        ("show 0005", "show", {"id": "0005"}),
        ("what's blocked", "blocked", {}),
        ("anything stuck?", "blocked", {}),
        ("zombies", "zombies", {}),
        ("digest", "digest", {}),
        ("what happened last night", "digest", {}),
        ("status", "status", {}),
        ("where are we", "status", {}),
        ("new platform-toolkit: badge shows stale status", "add_inbox",
         {"repo": "platform-toolkit", "desc": "badge shows stale status"}),
        ("add web-client = tighten the wall clock cap", "add_inbox",
         {"repo": "web-client", "desc": "tighten the wall clock cap"}),
        ("help", "help", {}),
        ("?", "help", {}),
    ],
)
def test_parse_hits(text: str, verb: str, args: dict[str, str]) -> None:
    got = grammar.parse(text)
    assert isinstance(got, Parsed)
    assert got.verb == verb
    assert got.args == args


@pytest.mark.parametrize("text", ["", "   ", "tell me a joke", "delete everything", "xyzzy"])
def test_parse_misses_return_unknown(text: str) -> None:
    assert isinstance(grammar.parse(text), Unknown)


@pytest.mark.parametrize(
    "text",
    [
        "new web-client: stop the digest from crashing",
        "add platform-toolkit: run the tests before commit",
        "queue app: plan out the new onboarding",
        "new repo: users can go back a page",
        "new repo: fix retry 0001 loop",
    ],
)
def test_task_description_with_a_control_word_still_files_a_task(text: str) -> None:
    got = grammar.parse(text)
    assert isinstance(got, Parsed) and got.verb == "add_inbox"


@pytest.mark.parametrize(
    ("text", "slug"),
    [("assign 0042 to go-live", "go-live"), ("move 7 run-loop", "run-loop"),
     ("put 3 under plan-b", "plan-b")],
)
def test_assign_to_a_move_named_after_a_control_word(text: str, slug: str) -> None:
    got = grammar.parse(text)
    assert isinstance(got, Parsed) and got.verb == "assign" and got.args["move"] == slug


def test_retry_without_an_id_is_not_a_retry() -> None:
    # a verb that needs an arg but matched none must fall through, not fire empty
    assert isinstance(grammar.parse("retry the thing"), Unknown)


def test_stop_wins_over_a_trailing_run_word() -> None:
    # order matters: "stop" is checked before "run"
    assert grammar.parse("stop the run").verb == "stop"


def test_help_text_lists_every_verb_shape() -> None:
    for token in ("stop", "go", "run", "retry", "clear", "show", "blocked", "digest", "new"):
        assert token in grammar.HELP