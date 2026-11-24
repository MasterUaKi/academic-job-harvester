# File: adapters/euraxess_nds_adapter.py
# Version: 0.3.3 | Release Date: 2026-11-24
# EN: Extracts verified direct individual job posting links (PDFs & detail pages) from
#     Lower Saxony & North-West German universities/research institutes and EURAXESS,
#     filtering out informational hub pages (/forschung, /akademische-karrierewege, etc.).
# DE: Extrahiert geprüfte direkte Einzel-Stellenlinks (PDFs & Detailseiten) von Hochschulen
#     und Forschungseinrichtungen in Niedersachsen/Nordwestdeutschland sowie EURAXESS.

from __future__ import annotations

import asyncio
import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

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


class EURAXESSAndNiedersachsenAdapter(SourceAdapter):
    """
    EN: Collects verified individual vacancy announcements (PDFs & job detail pages)
        across Lower Saxony & North-West German universities/research institutes and EURAXESS.
    DE: Erfasst geprüfte Einzel-Stellenausschreibungen (PDFs & Detailseiten) von Hochschulen
        und Forschungseinrichtungen in Niedersachsen/Nordwestdeutschland und EURAXESS.
    """

    source_id = "euraxess_niedersachsen"
    source_name = "Hochschulen Niedersachsen / Nordwest & EURAXESS"
    source_type = SourceType.FUNDING_OPPORTUNITY

    EURAXESS_FEEDS: Tuple[Tuple[str, str], ...] = (
        ("EURAXESS Jobs", "https://euraxess.ec.europa.eu/jobs/search/rss.xml"),
    )

    REGIONAL_JOB_PAGES: Tuple[Tuple[str, str, str], ...] = (
        ("Universität Vechta", "Vechta (Niedersachsen)", "https://karriere.uni-vechta.de/stellenangebote"),
        ("Carl von Ossietzky Universität Oldenburg", "Oldenburg (Niedersachsen)", "https://uol.de/stellen"),
        ("Leibniz Universität Hannover", "Hannover (Niedersachsen)", "https://www.uni-hannover.de/de/universitaet/arbeiten-an-der-uni-hannover/stellenangebote"),
        ("Technische Universität Braunschweig", "Braunschweig (Niedersachsen)", "https://www.tu-braunschweig.de/stellenmarkt"),
        ("Georg-August-Universität Göttingen", "Göttingen (Niedersachsen)", "https://www.uni-goettingen.de/de/305402.html"),
        ("Universität Bremen", "Bremen", "https://www.uni-bremen.de/universitaet/die-uni-als-arbeitgeber/offene-stellen"),
        ("Hochschule Bremen", "Bremen", "https://www.hs-bremen.de/ueber-uns/karriere/stellenangebote/"),
        ("Hochschule Bremerhaven", "Bremerhaven", "https://www.hs-bremerhaven.de/de/die-hochschule/stellenangebote"),
        ("Alfred-Wegener-Institut (AWI)", "Bremerhaven (Helmholtz)", "https://www.awi.de/arbeiten-lernen/jobs.html"),
    )

    POSTING_URL_RE = re.compile(
        r"(/job/|/jobs/[^/]+|/stellenangebote/[^/]+|/stelle/[^/]+|/stellenangebot/|"
        r"/karriere/[^/]+|/vacanc|/position/|/offene-stellen/[^/]+|/jobboerse/|"
        r"stellenangebote/.*\.pdf$|stellen/.*\.pdf$)",
        re.IGNORECASE,
    )

    EXCLUDED_HUB_RE = re.compile(
        r"(/forschung/?$|/akademische-karrierewege/|/infoportal|/ueber-uns/|/kontakt/|"
        r"/impressum|/datenschutz|/studium/|/lehre/|/presse/|/aktuelles/|/faq/?$|"
        r"/arbeiten-lernen/jobs\.html$)",
        re.IGNORECASE,
    )

    PAY_GRADE_RE = re.compile(
        r"\b(TV-?L\s*E?\s*1[345]|TVöD\s*E?\s*1[345]|E\s*1[345]|W\s*[123]|R[1234]|LfbA)\b",
        re.IGNORECASE,
    )

    WORKLOAD_RE = re.compile(
        r"\b(100\s*%|vollzeit|full[- ]?time|75\s*%|50\s*%|80\s*%|teilzeit)\b",
        re.IGNORECASE,
    )

    DEADLINE_RE = re.compile(
        r"(?:bewerbungsfrist|frist|bis zum|deadline)[:\s]*([0-3]?\d\.[01]?\d\.20\d{2}|20\d{2}-[01]\d-[0-3]\d)",
        re.IGNORECASE,
    )

    KENNZIFFER_RE = re.compile(
        r"(?:kennziffer|ref\.?|nr\.?)[:\s#]*([A-Z0-9][A-Z0-9\-_/]{2,24})",
        re.IGNORECASE,
    )

    def __init__(self, timeout_seconds: float = 14.0) -> None:
        self.timeout_seconds = timeout_seconds

    async def fetch_changes(self, cursor: Optional[str] = None) -> RawBatch:
        records: List[Dict[str, Any]] = []
        seen_urls: Set[str] = set()
        errors: List[str] = []

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds, connect=6.0),
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
                ),
                "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8",
            },
        ) as client:
            for label, feed_url in self.EURAXESS_FEEDS:
                try:
                    resp = await client.get(feed_url, timeout=8.0)
                    if resp.status_code == 200 and resp.text.strip():
                        for raw_item in self._parse_rss_or_html(
                            resp.text, feed_url, label, "EURAXESS Jobs", "EU / EURAXESS"
                        ):
                            norm = self.normalize(raw_item)
                            if norm and norm["url"] not in seen_urls:
                                seen_urls.add(norm["url"])
                                records.append(norm)
                except Exception as exc:
                    errors.append(f"EURAXESS '{label}': {exc}")

            for inst_name, loc_str, portal_url in self.REGIONAL_JOB_PAGES:
                try:
                    resp = await client.get(portal_url, timeout=9.0)
                    if resp.status_code == 200 and resp.text.strip():
                        for raw_item in self._parse_rss_or_html(
                            resp.text, portal_url, inst_name, inst_name, loc_str
                        ):
                            norm = self.normalize(raw_item)
                            if norm and norm["url"] not in seen_urls:
                                seen_urls.add(norm["url"])
                                records.append(norm)
                except Exception as exc:
                    errors.append(f"Portal '{inst_name}': {exc}")

        return RawBatch(
            source_id=self.source_id,
            source_type=self.source_type,
            records=records,
            next_cursor=None,
            metadata={
                "transport": "live_euraxess_nds_http",
                "verified_direct_links": len(records),
                "errors": errors,
            },
        )

    @classmethod
    def _is_real_vacancy_url(cls, candidate_url: str, page_url: str) -> bool:
        if not candidate_url.startswith(("https://", "http://")):
            return False
        norm_c = candidate_url.rstrip("/").lower()
        norm_p = page_url.rstrip("/").lower()
        if not norm_c or norm_c == norm_p:
            return False
        parsed = urlparse(candidate_url)
        if not parsed.path or parsed.path in ("/", "/de", "/de/", "/en", "/en/"):
            return False
        if cls.EXCLUDED_HUB_RE.search(parsed.path):
            return False
        path_and_query = f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path
        return bool(cls.POSTING_URL_RE.search(path_and_query))

    def _parse_rss_or_html(
        self,
        body: str,
        base_url: str,
        institution: str,
        source_portal: str,
        location: str,
    ) -> List[Dict[str, Any]]:
        found: List[Dict[str, Any]] = []
        seen_local: Set[str] = set()
        trimmed = body.lstrip()

        if trimmed.startswith(("<?xml", "<rss", "<feed")):
            try:
                root = ET.fromstring(trimmed)
                for item in root.findall(".//item")[:40]:
                    title_el = item.find("title")
                    link_el = item.find("link")
                    desc_el = item.find("description")
                    title = re.sub(r"\s+", " ", (title_el.text or "") if title_el is not None else "").strip()
                    link = urljoin(base_url, ((link_el.text or "") if link_el is not None else "").strip())
                    desc = re.sub(
                        r"\s+",
                        " ",
                        BeautifulSoup((desc_el.text or "") if desc_el is not None else "", "html.parser").get_text(" ", strip=True),
                    ).strip()
                    if len(title) >= 8 and self._is_real_vacancy_url(link, base_url) and link not in seen_local:
                        seen_local.add(link)
                        found.append({
                            "title": title,
                            "institution": institution,
                            "location": location,
                            "url": link,
                            "context": desc[:400],
                            "source_portal": source_portal,
                        })
                return found
            except Exception:
                pass

        soup = BeautifulSoup(body, "html.parser")
        for a in soup.find_all("a", href=True):
            raw_href = str(a["href"]).strip()
            if not raw_href or raw_href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            href = urljoin(base_url, raw_href).split("#")[0]
            if href in seen_local or not self._is_real_vacancy_url(href, base_url):
                continue

            txt = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
            txt_clean = re.sub(r"\s*\(PDF[^)]*\)\s*$", "", txt, flags=re.IGNORECASE).strip()
            if len(txt_clean) < 10 or len(txt_clean) > 220:
                continue

            parent = a.find_parent(["tr", "li", "article", "div"])
            ctx = re.sub(r"\s+", " ", parent.get_text(" ", strip=True)).strip() if parent else txt_clean

            seen_local.add(href)
            found.append({
                "title": txt_clean,
                "institution": institution,
                "location": location,
                "url": href,
                "context": ctx[:450],
                "source_portal": source_portal,
            })
        return found

    def normalize(self, raw_record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        title = re.sub(r"\s+", " ", str(raw_record.get("title") or "")).strip()
        url = str(raw_record.get("url") or raw_record.get("link") or "").strip()
        if len(title) < 8 or not url.startswith(("https://", "http://")):
            return None

        institution = re.sub(
            r"\s+",
            " ",
            str(raw_record.get("institution") or "Hochschule / Forschungseinrichtung"),
        ).strip()
        location = str(raw_record.get("location") or "Niedersachsen / Nordwest").strip()
        context = str(raw_record.get("context") or title).strip()

        combined = f"{title} {context} {institution}"
        grade_m = self.PAY_GRADE_RE.search(combined)
        pay_grade = re.sub(r"\s+", " ", grade_m.group(0)).upper() if grade_m else "TV-L / Tarif"

        wl_m = self.WORKLOAD_RE.search(combined)
        workload = re.sub(r"\s+", " ", wl_m.group(0)).upper() if wl_m else "Siehe Ausschreibung"

        dl_m = self.DEADLINE_RE.search(combined)
        deadline = dl_m.group(1).strip() if dl_m else "Siehe Ausschreibung"

        kz_m = self.KENNZIFFER_RE.search(combined)
        native_id = hashlib.sha256(url.lower().encode("utf-8")).hexdigest()[:16]
        kennziffer = kz_m.group(1).strip() if kz_m else native_id[:10].upper()

        horizon = "H2"
        de_date = re.match(r"^(\d{1,2})\.(\d{1,2})\.(20\d{2})$", deadline)
        if de_date:
            try:
                dt = datetime(int(de_date.group(3)), int(de_date.group(2)), int(de_date.group(1)), tzinfo=timezone.utc)
                days_left = (dt - datetime.now(timezone.utc)).days
                if days_left <= 2:
                    horizon = "H1"
                elif days_left <= 14:
                    horizon = "H2"
                else:
                    horizon = "H3"
            except Exception:
                pass

        tags = [
            tok.lower()
            for tok in re.findall(r"[A-Za-zÄÖÜäöüß0-9\-\+]{3,}", f"{title} {institution} {pay_grade}")
            if len(tok) >= 3
        ][:12]

        return {
            "id": f"nds-{native_id}",
            "native_id": native_id,
            "kennziffer": kennziffer,
            "title": title[:220],
            "institution": institution[:160],
            "funder": institution[:160],
            "location": location[:120],
            "pay_grade": pay_grade[:40],
            "workload": workload[:40],
            "contract": "PDF / Ausschreibung" if url.lower().endswith(".pdf") else "Direktlink",
            "horizon": horizon,
            "deadline": deadline[:40],
            "tags": tags,
            "link": url,
            "url": url,
            "description": context[:500],
            "source_portal": str(raw_record.get("source_portal") or self.source_name),
            "source_type": self.source_type.value,
        }


if __name__ == "__main__":
    async def _smoke() -> None:
        ad = EURAXESSAndNiedersachsenAdapter()
        batch = await ad.fetch_changes()
        print("Metadata:", batch.metadata)
        print(f"Verified direct-link vacancies found: {len(batch.records)}")
        for r in batch.records:
            print(f"  - [{r['institution']} | {r['pay_grade']}] {r['title']} -> {r['url']}")

    asyncio.run(_smoke())
