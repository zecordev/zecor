# SPDX-License-Identifier: Apache-2.0
"""Pull one JSON object out of a model reply that may carry code fences or prose."""

from __future__ import annotations

import json


def first_object(text: str) -> dict[str, object]:
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed: dict[str, object] = json.loads(text[start : i + 1])
                        return parsed
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    raise ValueError(f"no JSON object in reply:\n{text[:400]}")