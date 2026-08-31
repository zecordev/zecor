# SPDX-License-Identifier: Apache-2.0
"""`zecor console` -- the Command Center TUI.

Everything goes through `zecor.core.intents` / `zecor.core.view` -- the console owns no
state of its own. Refresh polls every second and rebuilds only when `snapshot_key`
changes, so an idle console costs nothing and never fights the cursor. A crash in the
refresh path is caught and shown as a toast -- the pane must never die.
"""

from __future__ import annotations

import contextlib
from typing import ClassVar

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.events import Resize
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, RichLog, Static, Tree

from zecor.console import converse, grammar, model, runner
from zecor.core import actionlog, intents, questions, roadmap, transcript, view


class _FieldPrompt(ModalScreen[dict[str, str] | None]):
    """A tiny form: a title and one Input per field. Enter submits, Esc cancels."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, fields: list[tuple[str, str]]) -> None:
        super().__init__()
        self._title = title
        self._fields = fields  # (key, placeholder)

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Label(self._title, id="prompt-title")
            for key, placeholder in self._fields:
                yield Input(placeholder=placeholder, id=f"f-{key}")
            yield Button("submit", variant="primary", id="prompt-submit")

    def on_mount(self) -> None:
        first = self._fields[0][0]
        self.query_one(f"#f-{first}", Input).focus()

    @on(Input.Submitted)
    @on(Button.Pressed, "#prompt-submit")
    def _submit(self) -> None:
        self.dismiss(
            {key: self.query_one(f"#f-{key}", Input).value.strip() for key, _ in self._fields}
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class _ConfirmScreen(ModalScreen[bool]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y,enter", "yes", "Yes"),
        Binding("n,escape", "no", "No"),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self._q = question

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Label(self._q, id="prompt-title")
            yield Label("y / n")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class _PickScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, options: list[str]) -> None:
        super().__init__()
        self._title = title
        self._options = options

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Label(self._title, id="prompt-title")
            for opt in self._options:
                yield Button(opt, id=f"pick-{opt}")

    @on(Button.Pressed)
    def _picked(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        self.dismiss(bid[len("pick-") :] if bid.startswith("pick-") else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class _DigestScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,q", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="digest-box"):
            yield Static(view.latest_digest(), id="digest-body")

    def action_dismiss(self) -> None:  # type: ignore[override]
        self.dismiss(None)


class _DetailScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,q", "dismiss", "Close")]

    def __init__(self, card_id: str) -> None:
        super().__init__()
        self._id: str = card_id

    def compose(self) -> ComposeResult:
        with Vertical(id="digest-box"):
            yield Static(self._body(), id="digest-body")

    def _body(self) -> str:
        cv = view.one(self._id)
        if cv is None:
            return f"no card {self._id}"
        lines = [
            f"card {cv.id}   [{cv.klass}/{cv.state}]" + ("  ZOMBIE" if cv.zombie else ""),
            f"repo: {cv.repo}",
            f"intent: {cv.intent}",
            f"rev {cv.rev}   retries {cv.retry_count}"
            + (f"   lease pid {cv.lease_pid}" if cv.lease_pid else ""),
            f"depends on: {', '.join(cv.depends_on) or '-'}",
            "",
            "recent attempts:",
        ]
        rows = view.attempts(self._id, limit=15)
        if not rows:
            lines.append("  (none)")
        for a in rows:
            lines.append(
                f"  {a.started_at[:19]}  {a.tier:4s} {a.model or '-':16s} "
                f"{a.outcome:10s} exit={a.verify_exit}  {a.reason}"
            )
        return "\n".join(lines)

    def action_dismiss(self) -> None:  # type: ignore[override]
        self.dismiss(None)


class _RoadmapScreen(ModalScreen[None]):
    """Mission -> Campaign -> Move -> Card, with a computed rollup on every node.
    Nothing here is stored; it is `roadmap.rollup()` over the live cards."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,q", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="digest-box"):
            yield Tree("roadmap", id="road")

    def on_mount(self) -> None:
        tree = self.query_one("#road", Tree)
        tree.root.expand()
        try:
            missions = roadmap.load()
            cards = view.cards()
        except Exception as e:
            tree.root.add_leaf(f"roadmap unreadable: {e}")
            return
        rolled = roadmap.rollup(missions, cards)
        if not rolled:
            tree.root.add_leaf("no roadmap/*.md yet")
        for r in rolled:
            self._add(tree.root, r)
        orphans = roadmap.orphan_cards(missions, cards)
        if orphans:
            node = tree.root.add(f"unassigned cards ({len(orphans)})")
            for cid in orphans:
                node.add_leaf(cid)

    def _add(self, parent: object, r: roadmap.Rolled) -> None:
        tag = "" if r.total == 0 else f"  {r.landed}/{r.total}"
        loud = "  <<< UNPLANNED" if r.status == "unplanned" else ""
        label = f"{r.status:>12} · {r.title}{tag}{loud}"
        node = parent.add(label, allow_expand=bool(r.children or r.cards))  # type: ignore[attr-defined]
        if r.status in ("landed",):
            node.collapse()
        for child in r.children:
            self._add(node, child)
        for cid in r.cards:
            node.add_leaf(f"· {cid}")

    def action_dismiss(self) -> None:  # type: ignore[override]
        self.dismiss(None)


class _ConsoleScreen(ModalScreen[None]):
    """A raw `zecor <args>` prompt. Output streams from a real subprocess so a bad
    argument or `--help` cannot take down the TUI; `run` is redirected to the detached
    `intents.run()` so a long job outlives this pane."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="console-box"):
            yield Label("zecor …  (esc to close)", id="console-title")
            yield RichLog(id="console-log", wrap=True, markup=False, max_lines=2000)
            yield Input(placeholder="status | check | digest | resources | plan …", id="console-in")

    def on_mount(self) -> None:
        self.query_one("#console-in", Input).focus()
        self.query_one("#console-log", RichLog).write("zecor console -- type a subcommand")

    @on(Input.Submitted)
    async def _submit(self, event: Input.Submitted) -> None:
        cmd = event.value.strip()
        self.query_one("#console-in", Input).value = ""
        if not cmd:
            return
        log = self.query_one("#console-log", RichLog)

        def emit(line: str) -> None:
            log.write(line)

        emit(f"$ zecor {cmd}")
        argv = runner.split(cmd)
        if argv and argv[0] in runner.DETACHED:
            opts = runner.run_opts(argv[1:])  # detach, but keep --until / --max
            emit(intents.run(max_n=opts.max_n, until=opts.until).message)
            return
        code = await runner.stream(argv, emit)
        emit(f"[exit {code}]")

    def action_dismiss(self) -> None:  # type: ignore[override]
        self.dismiss(None)


class _HistoryScreen(ModalScreen[None]):
    """The last day of zecor activity -- transcript turns + recent actions, one scroll."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,q", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="digest-box"):
            yield Static(self._body(), id="digest-body")

    def _body(self) -> str:
        lines = ["recent actions:"]
        for a in actionlog.recent(limit=20):
            flag = "undone" if a.undone else ("rev" if a.reversible else " - ")
            lines.append(f"  {a.ts[11:19]}  {flag:6}  {a.verb:12}  {a.args}")
        lines.append("")
        lines.append("conversation:")
        for t in transcript.tail(120):
            lines.append("  " + model.turn_line(t.role, t.kind, t.text).replace("\n", "\n  "))
        return "\n".join(lines)

    def action_dismiss(self) -> None:  # type: ignore[override]
        self.dismiss(None)


class _MetricsScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,q", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="digest-box"):
            yield Static(self._body(), id="digest-body")

    def _body(self) -> str:
        from zecor.core import metrics

        s = metrics.summary()
        if s.empty:
            return "no runs yet -- the attempt ledger is empty"
        out = [
            f"attempts {s.total_attempts}   lands {s.total_lands}   "
            f"est cost ${s.est_cost_usd:.2f}",
        ]
        if s.median_cycle_s is not None:
            out.append(f"cycle: median {s.median_cycle_s:.0f}s   p90 {s.p90_cycle_s:.0f}s")
        for t in s.by_tier:
            out.append(f"  {t.tier:5}  {t.lands:3}/{t.attempts:<3}  {t.rate:5.0%}")
        out.append("daily lands: " + metrics.sparkline([float(x) for x in s.daily_lands]))
        return "\n".join(out)

    def action_dismiss(self) -> None:  # type: ignore[override]
        self.dismiss(None)


class _DiffScreen(ModalScreen[None]):
    """Read-only view of `git diff main...zecor/<id>` for the selected card."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,q", "dismiss", "Close")]

    def __init__(self, card_id: str) -> None:
        super().__init__()
        self._card_id = card_id

    def compose(self) -> ComposeResult:
        with Vertical(id="digest-box"):
            yield Static(self._body(), id="digest-body")

    def _body(self) -> str:
        import subprocess

        from zecor.repos import RepoNotFound, resolve

        cv = view.one(self._card_id)
        if cv is None:
            return f"no card {self._card_id}"
        try:
            repo_dir = resolve(cv.repo)
        except RepoNotFound as e:
            return str(e)
        try:
            out = subprocess.run(
                ["git", "-C", str(repo_dir), "diff", f"main...zecor/{self._card_id}"],
                capture_output=True, text=True, timeout=20, check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            return f"git diff failed: {e}"
        return out.stdout or out.stderr or "(no diff -- branch missing or identical to main)"

    def action_dismiss(self) -> None:  # type: ignore[override]
        self.dismiss(None)


class ZecorConsole(App[None]):
    TITLE = "zecor console"
    CSS = """
    #statusbar { height: 1; padding: 0 1; background: $panel; color: $text; }
    #main { height: 1fr; }
    #chatcol { width: 2fr; }
    #transcript { height: 1fr; border: round $panel; padding: 0 1; }
    #chat { dock: bottom; border: tall $primary; }
    #cards { width: 1fr; height: 1fr; }
    #main.narrow #cards { display: none; }
    #main.narrow #chatcol { width: 1fr; }
    #main.wide #chatcol { width: 3fr; }
    #prompt-box {
        width: 70; height: auto; padding: 1 2;
        background: $surface; border: thick $primary;
    }
    #prompt-title { padding-bottom: 1; }
    #digest-box {
        width: 90%; height: 90%; padding: 1 2;
        background: $surface; border: thick $primary;
    }
    #digest-body { height: 1fr; overflow-y: scroll; }
    #console-box {
        width: 90%; height: 90%; padding: 1 2;
        background: $surface; border: thick $primary;
    }
    #console-log { height: 1fr; border: round $panel; }
    #console-in { dock: bottom; }
    #cmd { dock: bottom; display: none; }
    #cmd.on { display: block; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("colon", "command", "Command"),
        Binding("i", "focus_chat", "Chat"),
        Binding("s", "toggle_stop", "STOP/GO"),
        Binding("r", "retry", "Retry"),
        Binding("n", "new_task", "New"),
        Binding("g", "run", "Run"),
        Binding("p", "plan", "Plan"),
        Binding("z", "clear_zombie", "Un-zombie"),
        Binding("u", "undo", "Undo"),
        Binding("d", "digest", "Digest"),
        Binding("m", "roadmap", "Roadmap"),
        Binding("c", "console", "Console"),
        Binding("h", "history", "History"),
        Binding("M", "metrics", "Metrics"),
        Binding("v", "diff", "Diff"),
        Binding("f", "cycle_filter", "Filter"),
        Binding("R", "force_refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]
    _FILTERS = (None, "attention", "zombies")

    def __init__(self, *, read_only: bool = False, ambient: bool = False) -> None:
        super().__init__()
        self._key: tuple[object, ...] | None = None
        self._filter: str | None = None
        self._health: view.Health | None = None  # refreshed off-thread every 5s
        self._last_turn = 0
        self._repo_focus: str | None = None
        self._read_only = read_only
        self._ambient = ambient
        self._nlu_ok = True
        self._layout = "medium"
        self._momentum = ""
        self._known_landed: set[str] = set()  # for the "shipped" cue

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("loading…", id="statusbar")
        with Horizontal(id="main"):
            with Vertical(id="chatcol"):
                yield RichLog(id="transcript", wrap=True, markup=True, max_lines=4000)
                yield Input(id="chat", placeholder="talk to Zecor  ·  : for commands")
            yield DataTable(id="cards", zebra_stripes=True, cursor_type="row")
        yield Input(id="cmd", placeholder=f": {grammar.HELP}")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#cards", DataTable)
        table.add_columns(*model.COLUMNS)
        with contextlib.suppress(Exception):
            transcript.prune(keep_days=30)
        self._replay_transcript()
        self.set_interval(1.0, self._tick)  # card table -- cheap file reads
        self.set_interval(2.0, self._transcript_tick)  # pull new turns (watchd/narration)
        self.set_interval(5.0, self._health_tick)  # resource probes -- off the UI thread
        self._tick(force=True)
        self._health_tick()
        # the board drives the keyboard by default; chat is `i` (or auto in narrow).
        # call_after_refresh so this wins over Textual's post-mount auto-focus.
        self.call_after_refresh(self._focus_board)

    def _focus_board(self) -> None:
        with contextlib.suppress(Exception):
            if self._layout == "narrow" and not self._read_only:
                self.query_one("#chat", Input).focus()
            else:
                self.query_one("#cards", DataTable).focus()

    # -- transcript --------------------------------------------------------------

    def _replay_transcript(self) -> None:
        log = self.query_one("#transcript", RichLog)
        try:
            turns = transcript.tail(200)
        except Exception:
            return
        for t in turns:
            log.write(model.turn_line(t.role, t.kind, t.text))
        # from the turns we actually rendered -- a turn inserted after tail() but
        # before a separate latest_id() call would otherwise be skipped forever.
        self._last_turn = turns[-1].id if turns else 0
        if turns:
            log.write("[dim]— resumed —[/]")

    def _transcript_tick(self) -> None:
        with contextlib.suppress(Exception):
            new = transcript.since(self._last_turn, limit=200)
            if not new:
                return
            log = self.query_one("#transcript", RichLog)
            for t in new:
                log.write(model.turn_line(t.role, t.kind, t.text))
            self._last_turn = new[-1].id

    # -- refresh -------------------------------------------------------------

    def _ship_cue(self, cards: list[view.CardView]) -> None:
        """A brief cue when a card newly lands. Sound only if ZECOR_CONSOLE_SOUND is set."""
        import os

        landed = {c.id for c in cards if c.state == "landed"}
        fresh = landed - self._known_landed
        if self._known_landed and fresh:  # skip the first tick (everything looks 'new')
            for cid in sorted(fresh):
                self.notify(f"✦ shipped {cid}", timeout=4)
            if os.environ.get("ZECOR_CONSOLE_SOUND"):
                self.bell()
        self._known_landed = landed

    def _tick(self, *, force: bool = False) -> None:
        try:
            all_cards = view.cards()
        except Exception as e:  # a bad read must not kill the pane
            self.notify(f"read failed: {e}", severity="error")
            return
        with contextlib.suppress(Exception):
            questions.scan(all_cards)  # a blocked card -> a question turn (de-duped)
        self._ship_cue(all_cards)
        cards = self._apply_filter(all_cards)
        self._paint_status()
        try:
            key = model.snapshot_key(cards)
            if not force and key == self._key:
                return
            self._rebuild(cards)
            self._key = key  # only after a clean rebuild, so a failure retries next tick
        except Exception as e:
            self.notify(f"table refresh failed: {e}", severity="error")

    def _health_tick(self) -> None:
        # view.health() shells out to sysctl/vm_stat/pgrep/lms -- never on the UI thread
        self.run_worker(self._refresh_health(), exclusive=True, group="health")

    async def _refresh_health(self) -> None:
        import asyncio

        with contextlib.suppress(Exception):
            self._health = await asyncio.to_thread(view.health)
            from zecor import localmodel
            from zecor.core import metrics

            self._nlu_ok = await asyncio.to_thread(localmodel.available)
            s = await asyncio.to_thread(metrics.summary)
            today = s.daily_lands[-1] if s.daily_lands else 0
            self._momentum = model.momentum(
                s.total_lands, today, metrics.sparkline([float(x) for x in s.daily_lands])
            )
            self._paint_status()

    def _paint_status(self) -> None:
        with contextlib.suppress(Exception):  # status bar is cosmetic
            if self._health is None:
                line = "resources…"
            else:
                line = model.status_line(
                    self._health, nlu_ok=self._nlu_ok,
                    repo_focus=self._repo_focus, read_only=self._read_only,
                    momentum=self._momentum,
                )
            if self._filter:
                line += f"  [reverse] filter: {self._filter} [/]"
            self.query_one("#statusbar", Static).update(line)

    def _rebuild(self, cards: list[view.CardView]) -> None:
        table = self.query_one("#cards", DataTable)
        selected = self._selected_id()
        table.clear()
        seen: set[str] = set()
        for row in model.rows(cards):
            if row.key in seen:  # two files claiming one id -- DataTable would raise
                continue
            seen.add(row.key)
            table.add_row(*row.cells, key=row.key)
        if selected is not None and selected in table.rows:
            with contextlib.suppress(Exception):
                table.move_cursor(row=table.get_row_index(selected))

    _ATTENTION = frozenset({"gate-broken", "blocked", "needs-human-design", "unreadable"})

    def _apply_filter(self, cards: list[view.CardView]) -> list[view.CardView]:
        if self._repo_focus:
            cards = [c for c in cards if c.repo == self._repo_focus]
        if self._filter == "zombies":
            return [c for c in cards if c.zombie]
        if self._filter == "attention":
            return [c for c in cards if c.zombie or c.state in self._ATTENTION]
        return cards

    def _selected_id(self) -> str | None:
        table = self.query_one("#cards", DataTable)
        if not table.row_count:
            return None
        try:
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
            value = cell_key.row_key.value
            return value if isinstance(value, str) else None
        except Exception:
            return None

    # -- command bar (Layer 0 grammar) ---------------------------------------

    def action_command(self) -> None:
        bar = self.query_one("#cmd", Input)
        bar.add_class("on")
        bar.focus()

    def action_cycle_filter(self) -> None:
        i = self._FILTERS.index(self._filter) if self._filter in self._FILTERS else 0
        self._filter = self._FILTERS[(i + 1) % len(self._FILTERS)]
        self.notify(f"filter: {self._filter or 'all'}")
        self._tick(force=True)

    @on(Input.Submitted, "#cmd")
    def _run_command(self, event: Input.Submitted) -> None:
        bar = self.query_one("#cmd", Input)
        text = event.value
        bar.value = ""
        bar.remove_class("on")
        self.query_one("#cards", DataTable).focus()
        if not text.strip():
            return
        parsed = grammar.parse(text)
        if isinstance(parsed, grammar.Parsed):
            self._dispatch(parsed)
        else:
            self._fallback(text)

    # -- chat (transcript + Layer 2) --------------------------------------------

    def action_focus_chat(self) -> None:
        self.query_one("#chat", Input).focus()

    def on_resize(self, event: Resize) -> None:
        was = self._layout
        self._layout = model.layout_for(event.size.width)
        self.query_one("#main").set_classes(self._layout)
        # only steal focus on the *transition* into narrow, and only from the board --
        # never out of the command bar, the chat box, or a modal input.
        if self._layout == "narrow" and was != "narrow" and not self._read_only:
            with contextlib.suppress(Exception):
                if self.focused is self.query_one("#cards", DataTable):
                    self.query_one("#chat", Input).focus()

    @on(Input.Submitted, "#chat")
    def _on_chat(self, event: Input.Submitted) -> None:
        box = self.query_one("#chat", Input)
        text = event.value.strip()
        box.value = ""
        if not text:
            return
        if self._read_only:
            self.notify("read-only mode", severity="warning")
            return
        with contextlib.suppress(Exception):
            transcript.append("you", "message", text)
        # a control verb typed in chat is still Layer-0, never a model call
        parsed = grammar.parse(text)
        if isinstance(parsed, grammar.Parsed) and parsed.verb not in ("help",):
            with contextlib.suppress(Exception):
                transcript.append("zecor", "command", f"running: {parsed.verb}")
            self._dispatch(parsed)
            return
        self.run_worker(self._converse(text), exclusive=False, group="converse")

    async def _converse(self, text: str) -> None:
        import asyncio

        try:
            turns = await asyncio.to_thread(transcript.tail, 12)
            history = [(t.role, t.text) for t in turns]
            res = await asyncio.to_thread(converse.converse, text, history=history)
        except Exception as e:  # a Layer-2 failure must not kill the console
            with contextlib.suppress(Exception):
                transcript.append("zecor", "error", f"Layer-2 error: {e}")
            return
        if isinstance(res, converse.Unavailable):
            with contextlib.suppress(Exception):
                transcript.append("zecor", "notice", f"Layer-2 offline: {res.reason}")
            return
        with contextlib.suppress(Exception):
            transcript.append("zecor", "reply", res.text, meta={"via": res.via})
        for p in res.proposed:
            self._offer_proposal(p)

    def _offer_proposal(self, parsed: grammar.Parsed) -> None:
        pretty = parsed.verb + "".join(f" {k}={v}" for k, v in parsed.args.items())

        def _done(ok: bool | None) -> None:
            if ok:
                self._dispatch(parsed)

        self.push_screen(_ConfirmScreen(f"zecor proposes:  {pretty}\nrun it?"), _done)

    def action_undo(self) -> None:
        if self._read_only:
            self.notify("read-only mode", severity="warning")
            return
        res = actionlog.undo_last()
        self.notify(res.message, severity="information" if res.ok else "warning")
        self._tick(force=True)

    def _fallback(self, text: str) -> None:
        """Layer 0 missed -- ask the local router, then confirm before acting."""
        self.notify("asking the local model…", timeout=3)
        self.run_worker(self._classify(text), exclusive=True)

    async def _classify(self, text: str) -> None:
        import asyncio

        from zecor.console import nlu

        try:
            repo_names: list[str] | None = intents.repo_names()
        except Exception:
            repo_names = None
        res = await asyncio.to_thread(nlu.classify, text, repos=repo_names)
        if isinstance(res, nlu.Unavailable):
            self.notify(f"didn't understand: {text!r}  ({res.reason})", severity="warning")
        elif isinstance(res, nlu.Clarify):
            self._pick(res.options, text)
        else:  # a Parsed -- confirm before acting
            self._confirm(res)

    def _confirm(self, parsed: grammar.Parsed) -> None:
        pretty = parsed.verb + "".join(f" {k}={v}" for k, v in parsed.args.items())

        def _done(ok: bool | None) -> None:
            if ok:
                self._dispatch(parsed)

        self.push_screen(_ConfirmScreen(f"run:  {pretty} ?"), _done)

    def _pick(self, options: list[str], text: str) -> None:
        def _done(choice: str | None) -> None:
            if choice:
                self.action_command()
                bar = self.query_one("#cmd", Input)
                bar.value = choice + " "

        self.push_screen(_PickScreen(f"didn't understand {text!r} -- did you mean", options), _done)

    _READ_VERBS = frozenset(
        {"help", "digest", "status", "blocked", "zombies", "focus", "show"}
    )

    def _dispatch(self, parsed: grammar.Parsed) -> None:
        v, a = parsed.verb, parsed.args
        if self._read_only and v not in self._READ_VERBS:
            self.notify("read-only monitor mode -- no changes", severity="warning")
            return
        if v == "help":
            self.notify(grammar.HELP, timeout=12)
        elif v == "stop":
            self._notify_result(intents.stop())
            self._tick(force=True)
        elif v == "go":
            self._notify_result(intents.go())
            self._tick(force=True)
        elif v == "plan":
            self._notify_result(intents.plan())
        elif v == "run":
            self._notify_result(
                intents.run(max_n=int(a["max"]) if a.get("max") else None, until=a.get("until"))
            )
        elif v == "retry":
            self._notify_result(
                intents.retry(
                    a["id"], tier=a.get("tier"), model=a.get("model"), note=a.get("note")
                )
            )
            self._tick(force=True)
        elif v == "clear_zombie":
            self._notify_result(intents.clear_zombie(a["id"]))
            self._tick(force=True)
        elif v == "assign":
            self._notify_result(intents.assign_move(a["id"], a.get("move", "")))
            self._tick(force=True)
        elif v == "add_inbox":
            res = intents.add_inbox(a.get("repo", ""), a.get("desc", ""))
            self._notify_result(res)
            if res.ok:
                self._offer_plan()
        elif v == "digest":
            self.push_screen(_DigestScreen())
        elif v == "status":
            self._tick(force=True)
            self._health_tick()
            if self._health is not None:
                self.notify(model.status_line(self._health), timeout=8)
        elif v in ("blocked", "zombies"):
            self._filter = "attention" if v == "blocked" else "zombies"
            self._tick(force=True)
        elif v == "focus":
            want = a.get("repo", "").strip() or None
            self._repo_focus = None if want in ("all", "off", "clear") else want
            self.notify(f"focus: {self._repo_focus or 'all repos'}")
            self._tick(force=True)
        elif v == "undo":
            self.action_undo()
        elif v == "show":
            cv = view.one(a["id"])
            if cv is None:
                self.notify(f"no card {a['id']}", severity="warning")
            else:
                self.push_screen(_DetailScreen(a["id"]))

    # -- actions ----------------------------------------------------------------

    def _blocked_ro(self) -> bool:
        if self._read_only:
            self.notify("read-only monitor mode -- no changes", severity="warning")
            return True
        return False

    def action_force_refresh(self) -> None:
        self._tick(force=True)

    def action_history(self) -> None:
        self.push_screen(_HistoryScreen())

    def action_metrics(self) -> None:
        self.push_screen(_MetricsScreen())

    def action_diff(self) -> None:
        cid = self._selected_id()
        if cid is None:
            self.notify("no card selected", severity="warning")
            return
        self.push_screen(_DiffScreen(cid))

    def action_toggle_stop(self) -> None:
        if self._blocked_ro():
            return
        res = intents.go() if intents.is_stopped() else intents.stop()
        self.notify(res.message)
        self._tick(force=True)

    def action_run(self) -> None:
        if self._blocked_ro():
            return
        self._notify_result(intents.run())

    def action_plan(self) -> None:
        if self._blocked_ro():
            return
        self._notify_result(intents.plan())

    def action_digest(self) -> None:
        self.push_screen(_DigestScreen())

    def action_console(self) -> None:
        if self._blocked_ro():  # the raw `zecor <args>` pane can mutate
            return
        self.push_screen(_ConsoleScreen())

    def action_roadmap(self) -> None:
        self.push_screen(_RoadmapScreen())

    def action_clear_zombie(self) -> None:
        if self._blocked_ro():
            return
        cid = self._selected_id()
        if cid is None:
            self.notify("no card selected", severity="warning")
            return
        self._notify_result(intents.clear_zombie(cid))
        self._tick(force=True)

    def action_retry(self) -> None:
        if self._blocked_ro():
            return
        cid = self._selected_id()
        if cid is None:
            self.notify("no card selected", severity="warning")
            return

        def _done(vals: dict[str, str] | None) -> None:
            if vals is None:
                return
            res = intents.retry(
                cid,
                tier=vals.get("tier") or None,
                model=vals.get("model") or None,
                note=vals.get("note") or None,
            )
            self._notify_result(res)
            self._tick(force=True)

        self.push_screen(
            _FieldPrompt(
                f"retry {cid} -- all optional",
                [("tier", "tier: t1 t3 t4 t2b"), ("model", "model: opus …"), ("note", "note")],
            ),
            _done,
        )

    def action_new_task(self) -> None:
        if self._blocked_ro():
            return

        def _done(vals: dict[str, str] | None) -> None:
            if vals is None:
                return
            res = intents.add_inbox(vals.get("repo", ""), vals.get("desc", ""))
            self._notify_result(res)
            if res.ok:
                self._offer_plan()

        self.push_screen(
            _FieldPrompt(
                "new task -> queue/inbox.md",
                [("repo", "repo, e.g. platform-toolkit"), ("desc", "what needs doing")],
            ),
            _done,
        )

    def _offer_plan(self) -> None:
        def _done(ok: bool | None) -> None:
            if ok:
                self._notify_result(intents.plan())

        self.push_screen(_ConfirmScreen("run `zecor plan` now to turn it into a card?"), _done)

    def _notify_result(self, res: intents.Result) -> None:
        self.notify(res.message, severity="information" if res.ok else "error")


def run(*, read_only: bool = False, ambient: bool = False) -> int:
    ZecorConsole(read_only=read_only, ambient=ambient).run()
    return 0