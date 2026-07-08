"""Persist and load the tenant wage-type classification that drives import routing."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from app.header_utils import normalize_header
from app.master_sync import _read_classification
from app.models import PayrollImportClassification

# Normalized section tokens used by the classifier.
SECTION_EARNING = "EARNING"
SECTION_STATUTORY = "STATUTORY DEDUCTION"
SECTION_OTHER_DEDUCTION = "OTHER DEDUCTION"
SECTION_MEMO = "MEMO"
SECTION_CALCULATED = "CALCULATED"


def seed_classifications(session: Session, classification_content: bytes) -> int:
    """Replace the stored classification with the rows from the sheet. Returns row count."""
    rows = _read_classification(classification_content)
    now = datetime.utcnow()
    payload: list[dict[str, object]] = []
    seen: set[str] = set()
    for r in rows:
        norm = normalize_header(r["name"])
        if not norm or norm in seen:
            continue
        seen.add(norm)
        payload.append(
            {
                "normalized_header": norm,
                "raw_header": (r["name"] or "")[:512],
                "code": (r["code"] or "")[:64] or None,
                "section": (r["section"] or "")[:64],
                "nature": (r["nature"] or "")[:64] or None,
                "created_at": now,
                "updated_at": now,
            }
        )
    session.execute(delete(PayrollImportClassification))
    if payload:
        session.execute(insert(PayrollImportClassification), payload)
    return len(payload)


def load_classification_sections(session: Session) -> dict[str, str]:
    """normalized header -> normalized section. Empty dict when unseeded/absent."""
    try:
        rows = session.execute(
            select(
                PayrollImportClassification.normalized_header,
                PayrollImportClassification.section,
            )
        ).all()
    except Exception:
        return {}
    return {normalize_header(h): normalize_header(s) for h, s in rows if h and s}
