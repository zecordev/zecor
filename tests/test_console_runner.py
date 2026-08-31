# SPDX-License-Identifier: Apache-2.0
"""console.runner: streamed subprocess for the console pane (Command Center step 4)."""

from __future__ import annotations

import asyncio

from zecor.console import runner


def test_split_is_shlex_then_falls_back() -> None:
    assert runner.split("retry 0042 --tier t4") == ["retry", "0042", "--tier", "t4"]
    assert runner.split('ask "one two"') == ["ask", "one two"]
    assert runner.split('broken "quote') == ["broken", '"quote']


def test_run_opts_extracts_until_and_max() -> None:
    o = runner.run_opts(["--until", "06:30", "--max", "4"])
    assert o.until == "06:30" and o.max_n == 4
    assert runner.run_opts([]) == runner.RunOpts()
    assert runner.run_opts(["--max", "notanumber"]).max_n is None


def test_stream_captures_lines_and_exit_code() -> None:
    lines: list[str] = []

    async def _go() -> int:
        return await runner.stream(["--help"], lines.append)

    code = asyncio.run(_go())
    assert code == 0
    assert any("usage: zecor" in ln for ln in lines)


def test_stream_reports_a_bad_binary(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(runner, "zecor_bin", lambda: ["/nonexistent/zecor-bin-xyz"])
    lines: list[str] = []
    code = asyncio.run(runner.stream(["status"], lines.append))
    assert code == -1
    assert lines and lines[0].startswith("! could not start")