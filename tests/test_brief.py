# SPDX-License-Identifier: Apache-2.0
"""Round-trip + schema-conformance tests for the context-firewall brief."""

from __future__ import annotations

import json

import pytest

from zecor.brief import MAX_BRIEF_TOKENS, Brief, BriefError, load_schema, parse

GOOD = {
    "summary": "3 unit tests fail after the router change; all in the same file.",
    "failures": [
        {
            "test": "resolves nested route params",
            "file": "src/router/resolve.test.ts:88",
            "expected": "{id:'7'}",
            "actual": "{}",
            "first_divergence": (
                "src/router/resolve.ts:142 -- params map built before the segment split"
            ),
        }
    ],
    "already_ruled_out": ["config change", "dependency version", "test ordering"],
    "files_worth_reading": ["src/router/resolve.ts:120-160"],
    "question_for_escalation": "Is the segment split supposed to run before or after params?",
}


def test_parses_the_section_3_example() -> None:
    b = parse(json.dumps(GOOD))
    assert isinstance(b, Brief)
    assert len(b.failures) == 1
    assert b.failures[0].file.endswith(":88")
    assert not b.truncated


def test_question_that_names_a_decision_is_flagged() -> None:
    assert parse(json.dumps(GOOD)).names_a_decision() is True


def test_question_that_is_just_a_fact_is_not() -> None:
    d = {**GOOD, "question_for_escalation": "what does resolveRoute return"}
    assert parse(json.dumps(d)).names_a_decision() is False


def test_null_question_is_allowed() -> None:
    d = {**GOOD, "question_for_escalation": None}
    assert parse(json.dumps(d)).question_for_escalation is None


def test_missing_key_is_a_firewall_hole() -> None:
    d = {k: v for k, v in GOOD.items() if k != "failures"}
    with pytest.raises(BriefError, match="missing required keys"):
        parse(json.dumps(d))


def test_non_json_raises() -> None:
    with pytest.raises(BriefError, match="not valid JSON"):
        parse("not json at all")


def test_fat_brief_is_flagged_and_marked_truncated() -> None:
    d = {**GOOD, "summary": "x" * (MAX_BRIEF_TOKENS * 4 + 100)}
    b = parse(json.dumps(d))
    assert b.truncated
    assert b.flags


def test_schema_file_is_valid_json_and_matches_required_keys() -> None:
    schema = load_schema()
    assert schema["type"] == "object"
    required = schema["required"]
    assert isinstance(required, list)
    assert set(required) == set(GOOD.keys())