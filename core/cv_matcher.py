# File: core/cv_matcher.py
# Version: 0.9.0 | Release Date: 2026-11-24
# EN: Dedicated module for CV text extraction (PDF/DOCX/TXT), skill profile
#     persistence, and job-to-CV match scoring.
# DE: Eigenständiges Modul für Lebenslauf-Extraktion, Profilspeicherung und
#     Stellenabgleich.

from __future__ import annotations

import io
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

try:
    import pypdf
except ImportError:
    pypdf = None  # type: ignore

try:
    import docx
except ImportError:
    docx = None  # type: ignore


KNOWN_SKILL_LEXICON: List[str] = [
    "python", "dspace", "dspace-cris", "cris", "kdsf", "solr", "sqlite", "postgresql",
    "rest api", "docker", "typescript", "angular", "rdm", "fdm", "datacite", "doi",
    "dms", "sap", "ai", "llm", "knowledge graph", "forschungsförderung", "eu horizon",
    "projektmanagement", "innovation management", "transfer", "open source", "automation",
    "analytics", "german b2", "english", "forschung", "wissenschaft", "tv-l e13", "tv-l e14",
    "universität vechta", "niedersachsen", "research",
]


def extract_text_from_upload(filename: str, content: bytes) -> str:
    lower = filename.lower()
    if lower.endswith(".pdf") and pypdf is not None:
        reader = pypdf.PdfReader(io.BytesIO(content))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if lower.endswith(".docx") and docx is not None:
        doc = docx.Document(io.BytesIO(content))
        return "\n".join(p.text for p in doc.paragraphs)
    return content.decode("utf-8", errors="ignore")


def build_cv_profile(filename: str, raw_text: str, state_path: Path) -> Dict[str, Any]:
    clean = re.sub(r"\s+", " ", raw_text).strip()
    lower = clean.lower()
    detected_skills = [kw for kw in KNOWN_SKILL_LEXICON if kw in lower]
    if not detected_skills:
        words = re.findall(r"[A-Za-zÄÖÜäöüß\-]{4,}", lower)
        freq: Dict[str, int] = {}
        for w in words:
            freq[w] = freq.get(w, 0) + 1
        detected_skills = [k for k, _ in sorted(freq.items(), key=lambda x: -x[1])[:12]]

    profile = {
        "filename": filename,
        "uploaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "char_count": len(clean),
        "skills": detected_skills,
        "preview": clean[:240],
    }
    state_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    return profile


def load_cv_profile(state_path: Path) -> Dict[str, Any]:
    if state_path.is_file():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "filename": "Default Profile",
        "uploaded_at": "2026-11-24",
        "char_count": 0,
        "skills": ["python", "dspace-cris", "kdsf", "rest api", "rdm", "ai", "innovation management"],
        "preview": "Перетащите PDF/DOCX резюме сюда для расчёта совпадений.",
    }


def score_and_filter_jobs(
    jobs: List[Dict[str, Any]],
    cv_skills: List[str],
    query_filter: str = "",
    org_type_filter: str = "",
    institution_filter: str = "",
    pay_grade_filter: str = "",
    workload_filter: str = "",
    contract_filter: str = "",
    horizon_filter: str = "",
    sort_by: str = "match",
) -> List[Dict[str, Any]]:
    cv_set = {s.lower() for s in cv_skills}
    q = query_filter.lower().strip()
    org_q = org_type_filter.lower().strip()
    inst_q = institution_filter.lower().strip()
    grade_q = pay_grade_filter.lower().strip()
    wl_q = workload_filter.lower().strip()
    ct_q = contract_filter.lower().strip()
    hz_q = horizon_filter.upper().strip()
    enriched: List[Dict[str, Any]] = []

    for job in jobs:
        job_tags = [str(t).lower() for t in (job.get("tags") or [])]
        matched = [t for t in job_tags if t in cv_set]
        missing = [t for t in job_tags if t not in cv_set]
        score = int(round((len(matched) / max(1, len(job_tags))) * 100))

        inst_val = str(job.get("institution") or "")
        org_val = str(job.get("org_type") or "Академическая организация")
        grade_val = str(job.get("pay_grade") or "TV-L / Tarif")
        wl_val = str(job.get("workload") or "Siehe Ausschreibung")
        ct_val = str(job.get("contract") or "Ausschreibung")
        hz_val = str(job.get("horizon") or "H2").upper()

        if org_q and org_q != org_val.lower():
            continue
        if inst_q and inst_q not in inst_val.lower():
            continue
        if grade_q and grade_q not in grade_val.lower():
            continue
        if wl_q and wl_q not in wl_val.lower():
            continue
        if ct_q and ct_q not in ct_val.lower():
            continue
        if hz_q and hz_val != hz_q:
            continue

        haystack = " ".join([
            str(job.get("title") or ""),
            inst_val,
            org_val,
            str(job.get("location") or ""),
            grade_val,
            wl_val,
            ct_val,
            str(job.get("source_portal") or ""),
            *job_tags,
        ]).lower()

        if q:
            tokens = [tok for tok in re.split(r"[\s,;]+", q) if len(tok) >= 2]
            if tokens and not any(tok in haystack for tok in tokens):
                continue

        item = dict(job)
        item["match_score"] = min(99, max(20, score))
        item["matched_tags"] = matched
        item["missing_tags"] = missing[:3]
        enriched.append(item)

    if sort_by == "org_type":
        enriched.sort(key=lambda x: (str(x.get("org_type") or "").lower(), -x["match_score"]))
    elif sort_by == "institution":
        enriched.sort(key=lambda x: (str(x.get("institution") or "").lower(), -x["match_score"]))
    elif sort_by == "pay_grade":
        enriched.sort(key=lambda x: (str(x.get("pay_grade") or "").lower(), -x["match_score"]))
    elif sort_by == "workload":
        enriched.sort(key=lambda x: (str(x.get("workload") or "").lower(), -x["match_score"]))
    elif sort_by == "title":
        enriched.sort(key=lambda x: str(x.get("title") or "").lower())
    else:
        enriched.sort(key=lambda x: -x["match_score"])
    return enriched
