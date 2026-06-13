"""Shared CSV header normalization."""

from __future__ import annotations

import re


def normalize_header(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", str(s).strip().upper())
