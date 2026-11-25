# File: core/job_exporter.py
# Version: 0.9.0 | Release Date: 2026-11-24
# EN: Dedicated export module serializing filtered/scored vacancies into CSV
#     (UTF-8 BOM with semicolon separator) and JSON payloads.
# DE: Eigenständiges Exportmodul für gefilterte Stellenangebote im CSV- und JSON-Format.

from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, List


def render_jobs_json(jobs: List[Dict[str, Any]]) -> str:
    return json.dumps(jobs, ensure_ascii=False, indent=2)


def render_jobs_csv_bytes(jobs: List[Dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow([
        "match_score", "title", "org_type", "institution", "location",
        "pay_grade", "workload", "contract", "deadline", "kennziffer", "source_portal", "url",
    ])
    for j in jobs:
        writer.writerow([
            j.get("match_score", ""),
            j.get("title", ""),
            j.get("org_type", ""),
            j.get("institution", ""),
            j.get("location", ""),
            j.get("pay_grade", ""),
            j.get("workload", ""),
            j.get("contract", ""),
            j.get("deadline", ""),
            j.get("kennziffer", ""),
            j.get("source_portal", ""),
            j.get("url", ""),
        ])
    utf8_bom = chr(0xFEFF)
    return (utf8_bom + buf.getvalue()).encode("utf-8")
