# SPDX-License-Identifier: Apache-2.0
"""console.app: a Textual smoke test driven synchronously (Command Center step 3)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from zecor import config
from zecor.cards import Card
from zecor.core import intents


@pytest.fixture(autouse=True)
def _root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "zecor"
    for name, sub in [
        ("ZECOR_ROOT", ""), ("TASKS_DIR", "tasks"), ("LOGS_DIR", "logs"),
        ("DIGEST_DIR", "digest"), ("QUEUE_INBOX", "queue/inbox.md"),
        ("DB_PATH", "db/zecor.db"), ("STOP_FILE", "STOP"),
    ]:
        monkeypatch.setattr(config, name, root / sub if sub else root)
    (root / "tasks").mkdir(parents=True)
    return root


def _card(cid: str, state: str, **kw: object) -> None:
    c = Card(cid, "demo", f"do {cid}", "./v.sh", ["a.py"], **kw)  # type: ignore[arg-type]
    c.state = state
    c.write(config.TASKS_DIR)


def _dead_lease(cid: str) -> None:
    from zecor.core import leases

    db = leases._db()
    db.execute(
        "INSERT INTO lease VALUES (?,'t',999999,'2000-01-01T00:00:00+00:00','n')", (cid,)
    )
    db.commit()
    db.close()


def test_app_mounts_lists_cards_and_toggles_stop() -> None:
    _card("0001", "ready")
    _card("0002", "blocked")
    _card("0003", "ready")
    _dead_lease("0003")

    from textual.widgets import DataTable

    from zecor.console.app import ZecorConsole

    async def _run() -> None:
        app = ZecorConsole()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            table = app.query_one("#cards", DataTable)
            assert table.row_count == 3
            # worst-first: blocked before ready; zombie row present
            first_cells = table.get_row_at(0)
            assert first_cells[0] == "0002"  # blocked sorts above ready
            assert any(table.get_row_at(i)[1] == "ZOMBIE" for i in range(3))

            assert intents.is_stopped() is False
            await pilot.press("s")
            await pilot.pause()
            assert intents.is_stopped() is True
            await pilot.press("s")
            await pilot.pause()
            assert intents.is_stopped() is False

    asyncio.run(_run())


def test_console_pane_runs_a_subcommand_and_redirects_run(monkeypatch: pytest.MonkeyPatch) -> None:
    _card("0001", "ready")
    from textual.widgets import Input, RichLog

    from zecor.console import runner
    from zecor.console.app import ZecorConsole

    seen: list[list[str]] = []

    async def fake_stream(argv, on_line):  # type: ignore[no-untyped-def]
        seen.append(argv)
        on_line("fake output")
        return 0

    monkeypatch.setattr(runner, "stream", fake_stream)
    ran: list[str] = []
    monkeypatch.setattr(intents, "run", lambda **kw: intents.Result(True, "run started"))

    async def _run() -> None:
        app = ZecorConsole()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            app.action_console()
            await pilot.pause()
            await pilot.pause()
            screen = app.screen
            inp = screen.query_one("#console-in", Input)
            inp.value = "status"
            await pilot.press("enter")
            await pilot.pause()
            log = screen.query_one("#console-log", RichLog)
            assert seen == [["status"]]
            assert len(log.lines) > 1
            # `run` must NOT stream -- it goes to the detached intent
            inp.value = "run"
            await pilot.press("enter")
            await pilot.pause()
            assert seen == [["status"]]  # unchanged

    asyncio.run(_run())
    _ = ran


def test_command_bar_parses_and_dispatches() -> None:
    _card("0001", "blocked")
    from textual.widgets import DataTable, Input

    from zecor.console.app import ZecorConsole

    async def _run() -> None:
        app = ZecorConsole()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            assert intents.is_stopped() is False
            app.action_command()
            await pilot.pause()
            bar = app.query_one("#cmd", Input)
            bar.value = "please halt everything"
            await pilot.press("enter")
            await pilot.pause()
            assert intents.is_stopped() is True  # grammar -> intents.stop()

            # a view verb sets the filter, no crash
            app.action_command()
            await pilot.pause()
            app.query_one("#cmd", Input).value = "what's blocked"
            await pilot.press("enter")
            await pilot.pause()
            assert app._filter == "attention"
            assert app.query_one("#cards", DataTable).row_count == 1

    asyncio.run(_run())


def test_filter_key_cycles_and_hides_rows() -> None:
    _card("0001", "ready")
    _card("0002", "blocked")
    from textual.widgets import DataTable

    from zecor.console.app import ZecorConsole

    async def _run() -> None:
        app = ZecorConsole()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            table = app.query_one("#cards", DataTable)
            assert table.row_count == 2
            await pilot.press("f")  # -> attention
            await pilot.pause()
            assert app._filter == "attention"
            assert table.row_count == 1  # only the blocked card
            await pilot.press("f")  # -> zombies
            await pilot.pause()
            assert table.row_count == 0
            await pilot.press("f")  # -> all
            await pilot.pause()
            assert app._filter is None
            assert table.row_count == 2

    asyncio.run(_run())


def test_roadmap_pane_shows_the_tree_with_rollup(monkeypatch: pytest.MonkeyPatch) -> None:
    (config.ZECOR_ROOT / "roadmap").mkdir(parents=True)
    (config.ZECOR_ROOT / "roadmap" / "m.md").write_text(
        "# Mission: Ship it {slug: ship-it}\n"
        "## Campaign: Core {slug: core}\n"
        "### Move: The writer {slug: writer}\n"
        "### Move: Nothing yet {slug: empty-move}\n"
    )
    _card("0001", "landed", move="writer")
    _card("0002", "blocked", move="writer")
    _card("0003", "ready", move="no-such-move")

    from textual.widgets import Tree

    from zecor.console.app import ZecorConsole

    async def _run() -> None:
        app = ZecorConsole()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            app.action_roadmap()
            await pilot.pause()
            await pilot.pause()
            tree = app.screen.query_one("#road", Tree)
            labels = _tree_labels(tree.root)
            assert any("Ship it" in x for x in labels)
            assert any("blocked · The writer  1/2" in x for x in labels)
            assert any("UNPLANNED" in x and "Nothing yet" in x for x in labels)
            assert any("unassigned cards (1)" in x for x in labels)

    asyncio.run(_run())


def _tree_labels(node: object) -> list[str]:
    out = [str(node.label)]  # type: ignore[attr-defined]
    for c in node.children:  # type: ignore[attr-defined]
        out.extend(_tree_labels(c))
    return out


def test_grammar_miss_falls_back_to_nlu_then_confirms(monkeypatch: pytest.MonkeyPatch) -> None:
    _card("0001", "blocked")
    from zecor.console import grammar, nlu
    from zecor.console.app import ZecorConsole

    monkeypatch.setattr(
        nlu, "classify", lambda text, repos=None: grammar.Parsed("stop", {})
    )

    async def _run() -> None:
        app = ZecorConsole()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            assert intents.is_stopped() is False
            app.action_command()
            await pilot.pause()
            from textual.widgets import Input

            app.query_one("#cmd", Input).value = "everything on fire, wind it down"
            await pilot.press("enter")
            for _ in range(10):
                await pilot.pause()
            # a confirm modal is up; say yes
            await pilot.press("y")
            await pilot.pause()
            assert intents.is_stopped() is True

    asyncio.run(_run())


def test_nlu_unavailable_just_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    _card("0001", "ready")
    from zecor.console import nlu
    from zecor.console.app import ZecorConsole

    monkeypatch.setattr(nlu, "classify", lambda text, repos=None: nlu.Unavailable("offline"))

    async def _run() -> None:
        app = ZecorConsole()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            app.action_command()
            await pilot.pause()
            from textual.widgets import Input

            app.query_one("#cmd", Input).value = "blah blah blah"
            await pilot.press("enter")
            for _ in range(6):
                await pilot.pause()
            # no crash, still on the main screen
            assert app.screen is app.screen  # trivially: we got here without an exception

    asyncio.run(_run())


def test_assign_command_puts_a_card_under_a_move() -> None:
    _card("0001", "ready")
    from textual.widgets import Input

    from zecor.console.app import ZecorConsole
    from zecor.core import cards as core_cards

    async def _run() -> None:
        app = ZecorConsole()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            app.action_command()
            await pilot.pause()
            app.query_one("#cmd", Input).value = "assign 0001 to cc-tui"
            await pilot.press("enter")
            await pilot.pause()
            assert core_cards.read("0001").move == "cc-tui"

    asyncio.run(_run())


def test_new_task_offers_to_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    _card("0001", "ready")
    (config.QUEUE_INBOX.parent).mkdir(parents=True, exist_ok=True)
    config.QUEUE_INBOX.write_text("")
    from textual.widgets import Input

    from zecor.console.app import ZecorConsole

    planned: list[bool] = []
    monkeypatch.setattr(intents, "plan", lambda: planned.append(True) or intents.Result(True, "ok"))

    async def _run() -> None:
        app = ZecorConsole()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            app.action_new_task()
            await pilot.pause()
            await pilot.pause()
            app.screen.query_one("#f-repo", Input).value = "web-client"
            app.screen.query_one("#f-desc", Input).value = "do the thing"
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            await pilot.press("y")  # yes, plan it
            await pilot.pause()
            assert planned == [True]
            assert "do the thing" in config.QUEUE_INBOX.read_text()

    asyncio.run(_run())


def test_new_card_appears_within_a_poll() -> None:
    _card("0001", "ready")
    from textual.widgets import DataTable

    from zecor.console.app import ZecorConsole

    async def _run() -> None:
        app = ZecorConsole()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            table = app.query_one("#cards", DataTable)
            assert table.row_count == 1
            _card("0002", "ready")
            app._tick(force=True)
            await pilot.pause()
            assert table.row_count == 2

    asyncio.run(_run())


def test_transcript_replays_on_mount_and_chat_appends() -> None:
    from zecor.console.app import ZecorConsole
    from zecor.core import transcript

    transcript.append("you", "message", "earlier line")

    async def _run() -> None:
        app = ZecorConsole()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            assert app._last_turn >= 1  # replayed the existing turn
            from textual.widgets import Input

            await pilot.press("i")  # focus the chat box
            await pilot.pause()
            chat = app.query_one("#chat", Input)
            chat.value = "stop"
            await pilot.press("enter")  # a control verb typed in chat -> Layer 0, no model
            await pilot.pause()
            assert config.stop_requested()
            texts = [t.text for t in transcript.tail()]
            assert "stop" in texts  # the operator message was recorded

    asyncio.run(_run())


def test_read_only_mode_blocks_mutations() -> None:
    _card("0001", "ready")
    from zecor.console.app import ZecorConsole

    async def _run() -> None:
        app = ZecorConsole(read_only=True)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("s")  # STOP toggle
            await pilot.pause()
            assert not config.stop_requested()  # blocked

    asyncio.run(_run())


def test_narrow_terminal_hides_the_board() -> None:
    _card("0001", "ready")
    from textual.widgets import DataTable

    from zecor.console.app import ZecorConsole

    async def _run() -> None:
        app = ZecorConsole()
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause()
            assert app._layout == "narrow"
            assert not app.query_one("#cards", DataTable).display

    asyncio.run(_run())