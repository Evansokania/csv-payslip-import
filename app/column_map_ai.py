"""Azure OpenAI–assisted payroll CSV column classification (allowance vs deduction vs computed, etc.)."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, azure_openai_configured
from app.import_service import normalize_header
from app.models import Allowance, Deduction, PayrollImportColumnMap, PayrollImportRawRow


def _collect_header_samples(session: Session, run_id: int, *, max_rows: int = 12, samples_per_col: int = 5) -> dict[str, list[str]]:
    rows = session.scalars(
        select(PayrollImportRawRow)
        .where(PayrollImportRawRow.import_run_id == run_id)
        .order_by(PayrollImportRawRow.row_no)
        .limit(max_rows)
    ).all()
    acc: dict[str, list[str]] = defaultdict(list)
    for rr in rows:
        payload = rr.payload or {}
        if not isinstance(payload, dict):
            continue
        for k, raw in payload.items():
            if k is None:
                continue
            s = str(raw).strip()
            if not s or s.lower() in ("null", "none", "n/a", "-"):
                continue
            if len(s) > 120:
                s = s[:117] + "..."
            if s not in acc[k]:
                acc[k].append(s)
            if len(acc[k]) >= samples_per_col:
                continue
    return {k: v for k, v in acc.items()}


def _master_lookup_rows(session: Session) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    allowances = [{"id": a.id, "name": a.name} for a in session.scalars(select(Allowance).order_by(Allowance.name)).all()]
    deductions = [{"id": d.id, "name": d.name} for d in session.scalars(select(Deduction).order_by(Deduction.name)).all()]
    return allowances, deductions


def _resolve_master_id(
    name: str | None,
    *,
    by_norm: dict[str, int],
    names_ordered: list[str],
) -> int | None:
    if not name or not str(name).strip():
        return None
    n = normalize_header(str(name).strip())
    if n in by_norm:
        return by_norm[n]
    # loose: token overlap between AI suggestion and master names
    tokens = set(re.findall(r"[A-Za-z0-9]+", n))
    if not tokens:
        return None
    best: tuple[int, str] | None = None
    for cand in names_ordered:
        cn = normalize_header(cand)
        ct = set(re.findall(r"[A-Za-z0-9]+", cn))
        inter = len(tokens & ct)
        if inter == 0:
            continue
        score = inter / max(len(tokens | ct), 1)
        if best is None or score > best[0]:
            best = (score, cand)
    if best and best[0] >= 0.34:
        return by_norm.get(normalize_header(best[1]))
    return None


def _call_azure_json(settings: Settings, *, system: str, user: str) -> dict[str, Any]:
    if not azure_openai_configured(settings):
        raise RuntimeError(
            "Azure OpenAI is not configured. Set AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, "
            "and AZURE_OPENAI_DEPLOYMENT_NAME in .env"
        )
    url = (
        f"{settings.azure_openai_endpoint}/openai/deployments/{settings.azure_openai_deployment}"
        f"/chat/completions?api-version={settings.azure_openai_api_version}"
    )
    body: dict[str, Any] = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.15,
        "max_tokens": 4096,
        "response_format": {"type": "json_object"},
    }
    with httpx.Client(timeout=180.0) as client:
        r = client.post(
            url,
            headers={
                "api-key": settings.azure_openai_api_key,
                "Content-Type": "application/json",
            },
            json=body,
        )
    if r.status_code >= 400:
        raise RuntimeError(f"Azure OpenAI HTTP {r.status_code}: {r.text[:800]}")
    data = r.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Unexpected Azure response shape: {data!s}") from e
    parsed = json.loads(content)
    if isinstance(parsed, list):
        parsed = {"classifications": parsed}
    return parsed


def suggest_column_maps_with_azure(
    session: Session,
    settings: Settings,
    run_id: int,
) -> dict[str, Any]:
    """
    Call Azure OpenAI and return { "classifications": [...], "raw_model": {...} }.

    Each classification: header_normalized, role, allowance_name, deduction_name, confidence, rationale.
    """
    maps = session.scalars(select(PayrollImportColumnMap).where(PayrollImportColumnMap.import_run_id == run_id)).all()
    if not maps:
        raise ValueError("No column maps — upload CSV or rebuild column map first")

    samples = _collect_header_samples(session, run_id)
    allowances, deductions = _master_lookup_rows(session)
    # Trim very large master lists for token budget (IDs still applied via resolver)
    allowances_slim = allowances[:400]
    deductions_slim = deductions[:400]

    payload_for_model = {
        "columns": [
            {
                "header_raw": m.csv_header_raw,
                "header_normalized": m.csv_header_normalized,
                "current_role": m.role,
                "current_confidence": m.confidence,
                "sample_values": samples.get(m.csv_header_normalized, []),
            }
            for m in maps
        ],
        "allowance_masters": allowances_slim,
        "deduction_masters": deductions_slim,
        "rules": [
            "Each CSV column must get exactly one classification entry (same header_normalized).",
            "role must be one of: dimension, computed, earning, deduction, ignore.",
            "dimension = identifiers / keys only (payroll number, employee name, branch, department, SR no, dates as labels, etc.) — not money.",
            "computed = payroll totals / statutory / tax bases already aggregated (Gross, Net, PAYE, NSSF, NHIF, SHIF, Housing levy, Third rule, Total deduction, Tax charged, reliefs, etc.).",
            "earning = taxable or non-taxable pay components that increase pay (basic, allowances, overtime amounts, bonuses, commissions, etc.).",
            "deduction = amounts withheld from pay (loans, advances, absent, insurance employee share, etc.).",
            "ignore = blank, notes, or non-monetary junk.",
            "If role is earning and a master allowance clearly matches, set allowance_name to that master list name exactly; else null.",
            "If role is deduction and a master deduction clearly matches, set deduction_name to that master list name exactly; else null.",
            "Never invent master names — only names from the provided lists or null.",
            "Kenya payroll: NHIF/NSSF/SHIF/PAYE/AHL variants are usually computed unless clearly a loan repayment line.",
        ],
    }

    system = (
        "You are an expert payroll CSV mapping assistant. "
        "Return ONLY valid JSON with a top-level array key \"classifications\". "
        "Each item: header_normalized (string), role (string), allowance_name (string|null), "
        "deduction_name (string|null), confidence (number 0-1), rationale (short string). "
        "No markdown, no code fences."
    )
    user = json.dumps(payload_for_model, ensure_ascii=False)

    raw = _call_azure_json(settings, system=system, user=user)
    # Accept either {"classifications": [...]} or a bare array (repair)
    items = raw.get("classifications")
    if items is None and isinstance(raw.get("columns"), list):
        items = raw["columns"]
    if items is None:
        raise ValueError(f"Model JSON missing 'classifications': keys={list(raw.keys())}")

    if not isinstance(items, list):
        raise ValueError("classifications must be a list")

    return {"classifications": items, "raw_model": raw}


def apply_azure_classifications_to_maps(session: Session, run_id: int, classifications: list[dict[str, Any]]) -> int:
    """Persist AI suggestions onto PayrollImportColumnMap rows. Returns number of rows updated."""
    maps = session.scalars(select(PayrollImportColumnMap).where(PayrollImportColumnMap.import_run_id == run_id)).all()
    by_norm = {m.csv_header_normalized: m for m in maps}

    allowances = session.scalars(select(Allowance)).all()
    deductions = session.scalars(select(Deduction)).all()
    allow_by_norm = {normalize_header(a.name): a.id for a in allowances}
    ded_by_norm = {normalize_header(d.name): d.id for d in deductions}
    allow_names = [a.name for a in allowances]
    ded_names = [d.name for d in deductions]

    updated = 0
    for item in classifications:
        if not isinstance(item, dict):
            continue
        norm = normalize_header(str(item.get("header_normalized", "")))
        if not norm or norm not in by_norm:
            continue
        m = by_norm[norm]
        role = str(item.get("role") or "ignore").strip().lower()
        if role not in ("dimension", "computed", "earning", "deduction", "ignore"):
            role = "ignore"
        m.role = role
        m.allowance_id = None
        m.deduction_id = None
        if role == "earning":
            aid = _resolve_master_id(
                item.get("allowance_name"),
                by_norm=allow_by_norm,
                names_ordered=allow_names,
            )
            m.allowance_id = aid
        elif role == "deduction":
            did = _resolve_master_id(
                item.get("deduction_name"),
                by_norm=ded_by_norm,
                names_ordered=ded_names,
            )
            m.deduction_id = did
        conf = item.get("confidence")
        try:
            m.confidence = float(conf) if conf is not None else 0.92
        except (TypeError, ValueError):
            m.confidence = 0.92
        m.match_type = "ai_azure"
        updated += 1

    session.flush()
    return updated
