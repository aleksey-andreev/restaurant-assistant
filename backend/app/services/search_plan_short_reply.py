"""Whole-message classification for search-plan confirm step (Russian short replies)."""

from __future__ import annotations

import re
from typing import Literal

ShortReplyKind = Literal["affirm", "reject", "other"]


def _strip_trailing_punct(s: str) -> str:
    return re.sub(r"[\s.!?…]+$", "", s.strip())


def classify_search_plan_short_reply(text: str) -> ShortReplyKind:
    """
    Classify the last user message when awaiting plan confirmation.

    - ``affirm`` / ``reject``: only if the message is a *whole* short reply (no comma/semicolon/colon),
      to avoid treating «да, но в другом районе» as pure affirmation.
    """
    raw = (text or "").strip()
    if not raw:
        return "other"
    if any(ch in raw for ch in ",;:"):
        return "other"
    s = _strip_trailing_punct(raw.lower())
    if not s:
        return "other"

    affirm_re = re.compile(
        r"^(да|ок|окей|хорошо|давай|давайте|угу|ага|подтверждаю|верно|вс[её]\s*верно|все\s*верно|"
        r"согласен|согласна|согласны|поехали|запускай|впер[её]д|конечно|ладно)$",
        re.IGNORECASE | re.UNICODE,
    )
    if affirm_re.match(s):
        return "affirm"

    reject_re = re.compile(
        r"^(нет|неа|ноу|не\s+подходит|не\s+так|передумал|передумала|отмена|стоп|не\s+надо)$",
        re.IGNORECASE | re.UNICODE,
    )
    if reject_re.match(s):
        return "reject"

    return "other"
