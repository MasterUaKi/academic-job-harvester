# File: adapters/bundesagentur_adapter.py
# Version: 0.2.5 | Release Date: 2026-11-24
# EN: Queries Bundesagentur für Arbeit Jobsuche REST API (with angebotsart=1) and
#     academics.de job listings for direct posting URLs.
# DE: Fragt die Bundesagentur-Jobsuche-API (angebotsart=1) sowie academics.de nach
#     direkten Stellen-URLs ab.

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

try:
    from adapters.base import RawBatch, SourceAdapter, SourceType
except ImportError:
    class SourceType(str, Enum):
        FUNDING_OPPORTUNITY = "funding_opportunity"
        AWARDED_PROJECT = "awarded_project"
        FUNDER_REGISTRY = "funder_registry"

    @dataclass
    class RawBatch:
        source_id: str
        source_type: SourceType
        records: List[Dict[str, Any]] = field(default_factory=list)
        next_cursor: Optional[str] = None
        fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
        metadata: Dict[str, Any] = field(default_factory=dict)

    class SourceAdapter:
        source_id: str = "base"
        source_name: str = "Base Adapter"
        source_type: SourceType = SourceType.FUNDING_OPPORTUNITY

        async def fetch_changes(self, cursor: Optional[str] = None) -> RawBatch:
            raise NotImplementedError

        def normalize(self, raw_record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            raise NotImplementedError


class BundesagenturJobAdapter(SourceAdapter):
    source_id = "bundesagentur_jobsuche"
    source_name = "Bundesagentur für Arbeit & academics.de"
    source_type = SourceType.FUNDING_OPPORTUNITY

    API_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobs"
    ACADEMICS_JOBS_URL = "https://www.academics.de/jobs"

    DEFAULT_HEADERS = {
        "X-API-Key": "jobboerse-jobsuche",
        "Accept": "application/json, text/html, */*",
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
        ),
    }

    SEARCH_QUERIES: Tuple[str, ...] = (
        "Universität Forschung",
        "Wissenschaftlicher Mitarbeiter",
        "Forschungsreferent",
    )

    ACADEMIC_INSTITUTION_RE = re.compile(
        r"\b(universität|hochschule|tu |rwth|lmu|kit|charité|max-planck|max planck|"
        r"fraunhofer|helmholtz|leibniz|dlr|forschungszentrum|akademie der wissenschaften|"
        r"institut für|wissenschaft|forschung|transfer|cris|fdm|rdm|forschungsförderung|postdoc|professor)\b",
        re.IGNORECASE,
    )

    PAY_GRADE_RE = re.compile(
        r"\b(TV-?L\s*E?\s*1[345]|TVöD\s*E?\s*1[345]|E\s*1[345]|W\s*[123])\b",
        re.IGNORECASE,
    )

    WORKLOAD_RE = re.compile(
        r"\b(100\s*%|vollzeit|teilzeit|75\s*%|50\s*%|80\s*%)\b",
        re.IGNORECASE,
    )

    def __init__(
        self,
        size: int = 25,
        require_academic_match: bool = True,
        timeout_seconds: float = 12.0,
    ) -> None:
        self.size = size
        self.require_academic_match = require_academic_match
        self.timeout_seconds = timeout_seconds

    async def fetch_changes(self, cursor: Optional[str] = None) -> RawBatch:
        records: List[Dict[str, Any]] = []
        seen_links: Set[str] = set()
        errors: List[str] = []
        api_hits = 0
        html_hits = 0
        transports: List[str] = []

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds, connect=6.0),
            follow_redirects=True,
            headers=self.DEFAULT_HEADERS,
        ) as client:
            for query_str in self.SEARCH_QUERIES:
                params = {
                    "angebotsart": "1",
                    "was": query_str,
                    "page": "1",
                    "size": str(self.size),
                    "pav": "false",
                }
                try:
                    resp = await client.get(self.API_URL, params=params)
                    if resp.status_code != 200:
                        errors.append(f"BA API '{query_str}': HTTP {resp.status_code}")
                        continue
                    payload = resp.json()
                    raw_jobs = payload.get("stellenangebote") or []
                    api_hits += len(raw_jobs)
                    for raw_job in raw_jobs:
                        if not isinstance(raw_job, dict):
                            continue
                        norm = self.normalize(raw_job)
                        if norm and norm["url"] not in seen_links:
                            seen_links.add(norm["url"])
                            records.append(norm)
                except Exception as exc:
                    errors.append(f"BA API '{query_str}': {exc}")

            if api_hits > 0:
                transports.append(f"ba_rest_api({api_hits})")

            try:
                resp = await client.get(self.ACADEMICS_JOBS_URL, timeout=9.0)
                if resp.status_code == 200 and resp.text.strip():
                    for item in self._parse_academics_html(resp.text, self.ACADEMICS_JOBS_URL):
                        norm = self.normalize(item)
                        if norm and norm["url"] not in seen_links:
                            seen_links.add(norm["url"])
                            records.append(norm)
                            html_hits += 1
                else:
                    errors.append(f"academics.de: HTTP {resp.status_code}")
            except Exception as exc:
                errors.append(f"academics.de: {exc}")

            if html_hits > 0:
                transports.append(f"academics_html({html_hits})")

        return RawBatch(
            source_id=self.source_id,
            source_type=self.source_type,
            records=records,
            next_cursor=None,
            metadata={
                "transport": "+".join(transports) if transports else "none",
                "api_raw_hits": api_hits,
                "academics_hits": html_hits,
                "verified_direct_links": len(records),
                "errors": errors,
            },
        )

    def _parse_academics_html(self, html: str, base_url: str) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        found: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for a in soup.find_all("a", href=True):
            href = urljoin(base_url, str(a["href"]).strip()).split("#")[0]
            if href in seen or "/jobs/" not in href or href.rstrip("/") == base_url.rstrip("/"):
                continue
            title = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
            if len(title) < 10 or len(title) > 220:
                continue
            seen.add(href)
            found.append({
                "titel": title,
                "arbeitgeber": "academics.de",
                "externeUrl": href,
                "source_portal": "academics.de",
            })
        return found

    def _is_relevant_academic_job(self, text: str, employer: str) -> bool:
        if not self.require_academic_match:
            return True
        has_inst_or_sci = bool(self.ACADEMIC_INSTITUTION_RE.search(f"{employer} {text}"))
        has_grade = bool(self.PAY_GRADE_RE.search(text))
        return has_inst_or_sci or has_grade

    def normalize(self, raw_record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        title = re.sub(
            r"\s+",
            " ",
            str(raw_record.get("title") or raw_record.get("titel") or raw_record.get("beruf") or ""),
        ).strip()
        employer = re.sub(
            r"\s+",
            " ",
            str(raw_record.get("institution") or raw_record.get("arbeitgeber") or "Hochschule / Forschungsinstitut"),
        ).strip()
        if len(title) < 4:
            return None

        ext_url = str(raw_record.get("externeUrl") or raw_record.get("url") or raw_record.get("link") or "").strip()
        ref_nr = str(raw_record.get("refnr") or raw_record.get("referenznummer") or "").strip()

        if ext_url.startswith(("https://", "http://")):
            direct_url = ext_url
        elif ref_nr:
            direct_url = f"https://www.arbeitsagentur.de/jobsuche/suche?angebotsart=1&was={quote(ref_nr)}"
        else:
            return None

        arbeitsort = raw_record.get("arbeitsort") if isinstance(raw_record.get("arbeitsort"), dict) else {}
        ort = str(arbeitsort.get("ort") or "").strip()
        region = str(arbeitsort.get("region") or arbeitsort.get("bundesland") or "").strip()
        default_loc = (
            f"{ort} ({region})"
            if ort and region and region.lower() not in ort.lower()
            else (ort or region or "Deutschland")
        )
        location_str = str(raw_record.get("location") or default_loc).strip()

        combined_text = f"{title} {employer} {raw_record.get('beruf') or ''} {raw_record.get('beschreibung') or ''}"
        if not self._is_relevant_academic_job(combined_text, employer):
            return None

        grade_m = self.PAY_GRADE_RE.search(combined_text)
        pay_grade = re.sub(r"\s+", " ", grade_m.group(0)).upper() if grade_m else "TV-L / Tarif"

        wl_m = self.WORKLOAD_RE.search(combined_text)
        workload = re.sub(r"\s+", " ", wl_m.group(0)).upper() if wl_m else "100% / Vollzeit"

        contract = str(raw_record.get("befristung") or raw_record.get("arbeitszeitmodell") or "Siehe Ausschreibung").strip()
        deadline_raw = str(
            raw_record.get("aktuelleVeroeffentlichungsdatum")
            or raw_record.get("eintrittsdatum")
            or "Laufend / Siehe Ausschreibung"
        ).strip()
        iso_m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", deadline_raw)
        deadline_str = iso_m.group(1) if iso_m else deadline_raw[:40]

        native_id = ref_nr or hashlib.sha256(direct_url.encode("utf-8")).hexdigest()[:16]
        job_id = f"job-{native_id}"

        tokens = [
            tok.lower()
            for tok in re.findall(r"[A-Za-zÄÖÜäöüß0-9\-\+]{3,}", f"{title} {employer} {pay_grade}")
            if len(tok) >= 3
        ]
        dedup_tags: List[str] = []
        seen_t: Set[str] = set()
        for tok in tokens:
            if tok not in seen_t:
                seen_t.add(tok)
                dedup_tags.append(tok)

        return {
            "id": job_id,
            "native_id": native_id,
            "kennziffer": ref_nr or native_id[:12],
            "title": title[:220],
            "institution": employer[:160],
            "funder": employer[:160],
            "location": location_str[:120],
            "pay_grade": pay_grade[:40],
            "workload": workload[:40],
            "contract": contract[:60],
            "horizon": "H2",
            "deadline": deadline_str,
            "tags": dedup_tags[:12],
            "link": direct_url,
            "url": direct_url,
            "description": str(raw_record.get("beschreibung") or f"{title} — {employer} ({location_str}, {pay_grade}, {workload}).")[:600],
            "source_portal": str(raw_record.get("source_portal") or self.source_name),
            "source_type": self.source_type.value,
        }


if __name__ == "__main__":
    async def _smoke_test() -> None:
        adapter = BundesagenturJobAdapter(size=20)
        batch = await adapter.fetch_changes()
        print("Metadata:", batch.metadata)
        print(f"Real direct-link academic jobs: {len(batch.records)}")
        for job in batch.records[:8]:
            print(f"  - {job['title']} -> {job['url']}")

    asyncio.run(_smoke_test())
