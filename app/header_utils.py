"""Shared CSV/XLSX header normalization and synonym canonicalization."""

from __future__ import annotations

import re


def normalize_header(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", str(s).strip().upper())


# Real-world payroll exports label the same concept many ways. Map a *normalized*
# header to the canonical token the classifier / statutory engine understands, so
# the P9 / PAYE / NSSF logic recognizes them without manual re-mapping every import.
HEADER_SYNONYMS: dict[str, str] = {
    # Employee key
    "PERSONNEL NUMBER": "PAYROLL NO",
    "PERSONNEL NO": "PAYROLL NO",
    "PAYROLL NUMBER": "PAYROLL NO",
    "EMPLOYEE NUMBER": "PAYROLL NO",
    "EMP NO": "PAYROLL NO",
    "STAFF NO": "PAYROLL NO",
    "STAFF NUMBER": "PAYROLL NO",
    # SHIF (Social Health Insurance Fund) — successor to NHIF
    "SOCIAL HEALTH INS ACT": "SHIF",
    "SOCIAL HEALTH INSURANCE": "SHIF",
    "SOCIAL HEALTH INSURANCE FUND": "SHIF",
    "SHA": "SHIF",
    # Affordable Housing Levy
    "HOUSING LEVY": "AFFORDABLE HOUSING LEVY",
    "AHL": "AFFORDABLE HOUSING LEVY",
    # NSSF employee tiers I & II are the mandatory statutory contribution.
    "EE NSSF TIER I CONTRI": "NSSF TIER 1",
    "EE NSSF TIER II CONTRI": "NSSF TIER 2",
    "NSSF TIER I": "NSSF TIER 1",
    "NSSF TIER II": "NSSF TIER 2",
    # Tier III sits above the statutory upper limit — treated as a pension /
    # retirement contribution (feeds P9 "Other Pension Contribution"), per BIDCO.
    "EE NSSF TIER III CONTRI": "RETIREMENT CONTRIBUTION",
    "NSSF TIER III": "RETIREMENT CONTRIBUTION",
    # Explicit voluntary NSSF top-up — its own statutory column on payrolls.
    "NSSF VOL CONTRIBUTION": "VOLUNTARY NSSF",
    "NSSF VOLUNTARY CONTRIBUTION": "VOLUNTARY NSSF",
    # Net / gross variants
    "NETT PAY": "NET PAY",
}

_ROMAN = {"I": "1", "II": "2", "III": "3"}
_NSSF_TIER_RE = re.compile(r"NSSF\s+TIER\s+(III|II|I|[123])\b")


def canonical_header(s: str | None) -> str:
    """Normalize a header, then fold known synonyms to a canonical token.

    Falls back to the plain normalized header when nothing matches, so unknown
    columns behave exactly as before.
    """
    n = normalize_header(s)
    if not n:
        return ""
    if n in HEADER_SYNONYMS:
        return HEADER_SYNONYMS[n]
    # NSSF employee tier in any wording ("EE NSSF Tier I Contri", "NSSF Tier II", ...)
    m = _NSSF_TIER_RE.search(n)
    if m:
        tier = _ROMAN.get(m.group(1), m.group(1))
        # Tier III is above the statutory limit -> pension / retirement contribution.
        return "RETIREMENT CONTRIBUTION" if tier == "3" else f"NSSF TIER {tier}"
    if "SOCIAL HEALTH INS" in n:
        return "SHIF"
    return n
