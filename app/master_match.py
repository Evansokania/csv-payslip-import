"""Deterministic mapping of CSV allowance/deduction headers to existing tenant masters.

Links imported earning/deduction columns to a real ``allowances.id`` / ``deductions.id``
so their master attributes (taxable, in_basic, tax_rate, included_in_costing_report, …)
are applied. Statutory items (NSSF/PAYE/SHIF/AHL and anything ``is_statutory``) are
never linked here — they stay computed.

Match order (highest priority first):
  1. ``payroll_import_synonyms`` table (persistent header -> master id override)
  2. exact normalized master-name match
  3. conservative fuzzy token overlap (identity tokens only), threshold-gated
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from app.header_utils import normalize_header
from app.models import Allowance, Deduction, PayrollImportSynonym

# Statutory / computed concepts — never linked to a master (kept as computed columns).
_STATUTORY_NAMES = {
    normalize_header(x)
    for x in (
        "NSSF", "NSSF TIER 1", "NSSF TIER 2", "NSSF TIER 3", "VOLUNTARY NSSF",
        "PAYE", "PAYE DUE", "SHIF", "NHIF", "AFFORDABLE HOUSING LEVY", "HOUSING LEVY",
        "AHL", "PERSONAL RELIEF", "INSURANCE RELIEF", "TOTAL RELIEF", "SHIF RELIEF",
        "AHL RELIEF",
    )
}

# Generic tokens carry no identity when fuzzy-matching (every allowance is an "ALLOWANCE").
_GENERIC_TOKENS = {
    "ALLOWANCE", "ALLOWANCES", "ALLOW", "DEDUCTION", "DEDUCTIONS", "DED", "PAY", "PAYMENT",
    "CONTRIBUTION", "CONTRIB", "CONT", "LEVY", "EE", "ER", "EMPLOYEE", "EMPLOYER",
    "AMOUNT", "TOTAL", "MONTHLY", "THE", "OF", "AND",
}

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_FUZZY_THRESHOLD = 0.5


def _identity_tokens(s: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(normalize_header(s)) if t not in _GENERIC_TOKENS}


def _is_statutory_name(name: str) -> bool:
    return normalize_header(name) in _STATUTORY_NAMES


@dataclass
class MasterIndex:
    allow_by_norm: dict[str, int] = field(default_factory=dict)
    ded_by_norm: dict[str, int] = field(default_factory=dict)
    allow_tokens: list[tuple[set[str], int]] = field(default_factory=list)
    ded_tokens: list[tuple[set[str], int]] = field(default_factory=list)
    syn_allow: dict[str, int] = field(default_factory=dict)
    syn_ded: dict[str, int] = field(default_factory=dict)

    def has_masters(self) -> bool:
        return bool(self.allow_by_norm or self.ded_by_norm)


def load_master_index(session: Session) -> MasterIndex:
    """Build the non-statutory master index + persistent synonym overrides."""
    idx = MasterIndex()
    insp = inspect(session.get_bind())

    ded_has_stat = False
    if insp.has_table("deductions"):
        ded_cols = {c["name"].lower() for c in insp.get_columns("deductions")}
        ded_has_stat = "is_statutory" in ded_cols
        sel = "id, name, is_statutory" if ded_has_stat else "id, name"
        for r in session.execute(text(f"SELECT {sel} FROM deductions")).mappings():
            name = str(r["name"] or "")
            if not name:
                continue
            if (ded_has_stat and int(r.get("is_statutory") or 0) == 1) or _is_statutory_name(name):
                continue
            did = int(r["id"])
            idx.ded_by_norm.setdefault(normalize_header(name), did)
            tk = _identity_tokens(name)
            if tk:
                idx.ded_tokens.append((tk, did))

    for a in session.scalars(select(Allowance)).all():
        name = str(a.name or "")
        if not name or _is_statutory_name(name):
            continue
        aid = int(a.id)
        idx.allow_by_norm.setdefault(normalize_header(name), aid)
        tk = _identity_tokens(name)
        if tk:
            idx.allow_tokens.append((tk, aid))

    # Persistent header -> master overrides (optional table; ignore if absent).
    try:
        for s in session.scalars(select(PayrollImportSynonym)).all():
            h = normalize_header(s.normalized_header)
            if not h:
                continue
            if s.allowance_id:
                idx.syn_allow[h] = int(s.allowance_id)
            if s.deduction_id:
                idx.syn_ded[h] = int(s.deduction_id)
    except Exception:
        pass

    return idx


def _fuzzy(tokens: set[str], pool: list[tuple[set[str], int]]) -> int | None:
    """Best master for a header's identity tokens.

    A master whose identity tokens are fully contained in the header wins (most
    specific such master); otherwise fall back to Jaccard token overlap.
    """
    if not tokens:
        return None
    subset_best: tuple[int, int] | None = None  # (master token count, id)
    jaccard_best: tuple[float, int] | None = None
    for tk, mid in pool:
        if not tk:
            continue
        if tk <= tokens:  # master name fully present in the header
            if subset_best is None or len(tk) > subset_best[0]:
                subset_best = (len(tk), mid)
        inter = len(tokens & tk)
        if inter:
            score = inter / len(tokens | tk)
            if jaccard_best is None or score > jaccard_best[0]:
                jaccard_best = (score, mid)
    if subset_best is not None:
        return subset_best[1]
    if jaccard_best is not None and jaccard_best[0] >= _FUZZY_THRESHOLD:
        return jaccard_best[1]
    return None


def match_earning(header: str, idx: MasterIndex) -> int | None:
    """Return an allowance master id for a (non-statutory) earning header, or None."""
    h = normalize_header(header)
    if h in idx.syn_allow:
        return idx.syn_allow[h]
    if h in idx.allow_by_norm:
        return idx.allow_by_norm[h]
    return _fuzzy(_identity_tokens(h), idx.allow_tokens)


def match_deduction(header: str, idx: MasterIndex) -> int | None:
    """Return a deduction master id for a (non-statutory) deduction header, or None."""
    h = normalize_header(header)
    if h in idx.syn_ded:
        return idx.syn_ded[h]
    if h in idx.ded_by_norm:
        return idx.ded_by_norm[h]
    return _fuzzy(_identity_tokens(h), idx.ded_tokens)
