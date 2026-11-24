# File: skills_registry/job_skill_universit_t_g_ttingen_stellenangebote.py
# Version: 0.4.1 | Release Date: 2026-11-24
# Source: Universität Göttingen Stellenangebote
# Auto-crystallized and AST-verified by PyCognition SkillCrystallizer.

"""
Deterministic extractor for academic/university job postings.

Target example: Universität Göttingen – Stellenanzeigen
(https://www.uni-goettingen.de/de/305402.html)

The page renders its vacancies in a table (#vacancies) whose rows carry the
columns: ID | Titel | Institut | Entgelt/Besoldung | Bewerbungsfrist |
Besetzungsdatum | Befristung.  When the table is empty (JS-rendered or no
open positions), the extractor falls back to scanning the article body for
individual job-detail pages / job PDFs, strictly rejecting navigation,
imprint, privacy, social-media and general overview links.
"""

import re
import datetime
import html as html_module
from typing import Dict, List, Optional
from urllib.parse import urljoin
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

DEFAULT_INSTITUTION = "Georg-August-Universität Göttingen"

# Fragments that must never appear in an accepted job link.
_BLOCKED_HREF_FRAGMENTS = (
    "impressum", "imprint", "datenschutz", "datenschutzgrundverordnung",
    "privacy", "dsgvo", "hinweisblatt", "barrierefreiheit", "accessibility",
    "policies.google", "facebook.com", "instagram.com", "youtube.com",
    "linkedin.com", "bsky.app", "mastodon", "twitter.com", "x.com/",
    "xing.com", "ecampus.uni-goettingen.de", "studip.uni-goettingen.de",
    "intern.uni-goettingen.de", "academiccloud", "chatai", "flexnow",
    "stellenwerk-goettingen.de", "/de/439479.html", "/de/635204.html",
    "/de/545353.html", "/de/100652.html", "/de/1.html", "/de/305223.html",
    "/de/305402.html", "/de/699099.html", "print-logo", "logo.svg",
    "fidh_logo", "bewerbungen@zvw",
)

# Link texts that identify navigational chrome rather than a job.
_NAV_LINK_WORDS = (
    "impressum", "imprint", "datenschutz", "privacy", "barrierefreiheit",
    "kontakt", "contact", "suche", "search", "menü", "menu", "startseite",
    "homepage", "english", "deutsch", "schließen", "close", "zum ecampus",
    "erklärvideo", "interne stellen", "jobportal", "stellenausschreibungen",
    "stellenanzeigen", "anzeigenschaltung", "social", "newsletter",
    "sitemap", "inhaltsverzeichnis", "alumni", "intranet", "login",
)

# Hints that a URL points at an individual posting.
_JOB_HREF_HINTS = (
    "stellenausschreibung", "stellenanzeige", "stellenangebot",
    "ausschreibung", "vacanc", "job", "karriere", "bewerbung",
)

_WORKLOAD_RE = re.compile(
    r"(\d{1,3}(?:[.,]\d+)?)\s*"
    r"(?:%|v\.\s?h\.|v\.h\.|stunden(?:/woche|\s*pro\s*woche)?|"
    r"std\.?(?:/woche)?|wochenstunden|hours?(?:/week)?|h/week)",
    re.IGNORECASE,
)

_PAY_PATTERNS = (
    re.compile(r"\b(?:TV-?L|TVL|TV-?öD|TVöD|TV-?H|TV-?ÄD)\s*[-/]?\s*"
               r"(?:E\s*)?\d{1,2}(?:\s*(?:Ü|L|Fallgruppe)\s*\d{1,2})?", re.IGNORECASE),
    re.compile(r"\bEntgeltgruppe\s*\d{1,2}(?:\s*(?:Ü|TV-?L))?", re.IGNORECASE),
    re.compile(r"\bBesoldungsgruppe\s*[A-Z]\s*\d{1,2}", re.IGNORECASE),
    re.compile(r"\bW\s?[1-3](?:\s*\(?\s*(?:TV-?L|Besoldung)\s*\)?)?", re.IGNORECASE),
    re.compile(r"\bE\s?\d{1,2}\s*(?:TV-?L|TVöD)\b", re.IGNORECASE),
    re.compile(r"\bA\s?\d{1,2}\b"),
)

_DATE_RE = re.compile(
    r"(\d{1,2})\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{2,4})"
)

_MONTHS = {
    "januar": 1, "jan": 1, "jänner": 1,
    "februar": 2, "feb": 2,
    "märz": 3, "maerz": 3, "mrz": 3, "mar": 3,
    "april": 4, "apr": 4,
    "mai": 5,
    "juni": 6, "jun": 6,
    "juli": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "oktober": 10, "okt": 10,
    "november": 11, "nov": 11,
    "dezember": 12, "dez": 12,
}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _clean(value) -> str:
    """Normalise whitespace / entities in an arbitrary text fragment."""
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = str(value)
        except Exception:
            return ""
    text = html_module.unescape(value)
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _norm_date(value: str) -> str:
    """Return an ISO-8601 date string (YYYY-MM-DD) or ''."""
    text = _clean(value)
    if not text:
        return ""

    match = _DATE_RE.search(text)
    if match:
        day = int(match.group(1))
        month = int(match.group(2))
        year = int(match.group(3))
        if year < 100:
            year += 2000
        try:
            return datetime.date(year, month, day).isoformat()
        except ValueError:
            return ""

    match = re.search(r"(\d{1,2})\.?\s+([A-Za-zäöüÄÖÜß]+)\s+(\d{4})", text)
    if match:
        month = _MONTHS.get(match.group(2).lower())
        if month:
            try:
                return datetime.date(
                    int(match.group(3)), month, int(match.group(1))
                ).isoformat()
            except ValueError:
                return ""
    return ""


def _find_workload(text: str) -> str:
    """Extract 'Teilzeit 50 %' / '19,5 Stunden/Woche' / 'Vollzeit' etc."""
    if not text:
        return ""
    match = _WORKLOAD_RE.search(text)
    if match:
        return _clean(match.group(0))
    lowered = text.lower()
    if "vollzeit" in lowered:
        return "Vollzeit"
    if "teilzeit" in lowered:
        return "Teilzeit"
    return ""


def _find_pay_grade(text: str) -> str:
    """Extract a German pay grade (TV-L E13, Besoldung W2, A13, ...)."""
    if not text:
        return ""
    for pattern in _PAY_PATTERNS:
        match = pattern.search(text)
        if match:
            return _clean(match.group(0))
    return ""


def _is_blocked_href(href: str) -> bool:
    """True for anchors, navigation, legal, social and contact links."""
    if not href:
        return True
    lowered = href.strip().lower()
    if lowered.startswith(("#", "mailto:", "javascript:", "tel:", "callto:")):
        return True
    for fragment in _BLOCKED_HREF_FRAGMENTS:
        if fragment in lowered:
            return True
    return False


def _has_job_hint(href: str) -> bool:
    """True when the URL itself signals an individual posting."""
    if not href:
        return False
    lowered = href.lower()
    if lowered.split("?")[0].endswith(".pdf"):
        return True
    for hint in _JOB_HREF_HINTS:
        if hint in lowered:
            return True
    return False


def _is_nav_text(text: str) -> bool:
    """Reject short / navigational anchor labels used as job titles."""
    cleaned = _clean(text)
    if len(cleaned) < 6:
        return True
    lowered = cleaned.lower().strip(" .:»«-–")
    for word in _NAV_LINK_WORDS:
        if lowered == word or lowered.startswith(word):
            return True
    return False


def _content_root(soup: BeautifulSoup):
    """Narrow the parse to the article area, dropping chrome."""
    node = soup.find("article")
    if node is None:
        node = soup.find(id="page-content")
    if node is None:
        node = soup.find(id="main")
    if node is None:
        node = soup.find("main")
    if node is None:
        node = soup.body if soup.body is not None else soup
    return node


# --------------------------------------------------------------------------
# Table handling
# --------------------------------------------------------------------------

def _col_key(header_text: str) -> str:
    """Map a German/English column header onto an internal field name."""
    lowered = _clean(header_text).lower()
    if lowered in ("id", "nr.", "nr", "nummer"):
        return "id"
    if "titel" in lowered or "title" in lowered or "stelle" in lowered:
        return "title"
    if "institut" in lowered or "institution" in lowered or "einrichtung" in lowered \
            or "fakultät" in lowered or "fakultaet" in lowered:
        return "institution"
    if "entgelt" in lowered or "besoldung" in lowered or "vergütung" in lowered \
            or "verguetung" in lowered or "pay" in lowered or "salary" in lowered:
        return "pay_grade"
    if "frist" in lowered or "deadline" in lowered or "bewerbungsschluss" in lowered:
        return "deadline"
    if "besetzungsdatum" in lowered or "start" in lowered or "beginn" in lowered:
        return "start_date"
    if "befristung" in lowered or "vertrag" in lowered or "contract" in lowered:
        return "contract"
    if "arbeitszeit" in lowered or "umfang" in lowered or "workload" in lowered \
            or "wochenstunden" in lowered:
        return "workload"
    return ""


def _header_map(table) -> Dict[str, int]:
    """Map internal field name -> column index."""
    headers = table.find_all("th")
    if not headers:
        first_row = table.find("tr")
        if first_row is not None:
            headers = first_row.find_all("td")
    mapping: Dict[str, int] = {}
    for index, header in enumerate(headers):
        key = _col_key(header.get_text(" ", strip=True))
        if key and key not in mapping:
            mapping[key] = index
    return mapping


_DEFAULT_COLUMNS = {
    "id": 0,
    "title": 1,
    "institution": 2,
    "pay_grade": 3,
    "deadline": 4,
    "start_date": 5,
    "contract": 6,
}


def _candidate_tables(root) -> List:
    """Vacancy tables: the id-tagged one plus any tabular marking-like list."""
    tables = []
    tagged = root.find("table", id="vacancies")
    if tagged is not None:
        tables.append(tagged)

    for table in root.find_all("table"):
        if table is tagged:
            continue
        head_text = " ".join(
            th.get_text(" ", strip=True) for th in table.find_all("th")
        ).lower()
        if and_headers := (
            ("titel" in head_text or "title" in head_text)
            and ("institut" in head_text or "institution" in head_text
                 or "frist" in head_text or "deadline" in head_text)
        ):
            if and_headers:
                tables.append(table)
    return tables


def _rows_from_table(table, base_url: str) -> List[Dict[str, str]]:
    records: List[Dict[str, str]] = []

    mapping = _header_map(table)
    if len(mapping) < 3:
        mapping = dict(_DEFAULT_COLUMNS)

    body_rows = []
    tbody = table.find("tbody")
    if tbody is not None:
        body_rows = tbody.find_all("tr")
    if not body_rows:
        body_rows = [row for row in table.find_all("tr") if row.find("td")]

    for row in body_rows:
        cells = row.find_all("td")
        if not cells:
            continue
        if not any(_clean(cell.get_text(" ", strip=True)) for cell in cells):
            continue

        def cell(key: str):
            index = mapping.get(key)
            if index is None or index >= len(cells):
                return None
            return cells[index]

        title_cell = cell("title")
        anchor = None
        if title_cell is not None:
            anchor = title_cell.find("a", href=True)

        title = _clean(title_cell.get_text(" ", strip=True)) if title_cell is not None else ""
        if not title and anchor is not None:
            title = _clean(anchor.get_text(" ", strip=True))
        if not title:
            continue

        # Resolve the detail link for this posting.
        anchors = []
        if title_cell is not None:
            anchors.extend(title_cell.find_all("a", href=True))
        anchors.extend(row.find_all("a", href=True))

        link = base_url
        for candidate in anchors:
            href = (candidate.get("href") or "").strip()
            if _is_blocked_href(href):
                continue
            link = urljoin(base_url, href)
            break

        row_text = _clean(row.get_text(" ", strip=True))

        institution = ""
        if cell("institution") is not None:
            institution = _clean(cell("institution").get_text(" ", strip=True))
        if not institution:
            institution = DEFAULT_INSTITUTION

        pay_grade = ""
        if cell("pay_grade") is not None:
            pay_grade = _clean(cell("pay_grade").get_text(" ", strip=True))
        if not pay_grade:
            pay_grade = _find_pay_grade(row_text)

        workload = ""
        if cell("workload") is not None:
            workload = _clean(cell("workload").get_text(" ", strip=True))
        if not workload:
            workload = _find_workload(row_text + " " + title)

        deadline = ""
        if cell("deadline") is not None:
            deadline = _norm_date(cell("deadline").get_text(" ", strip=True))

        start_date = ""
        if cell("start_date") is not None:
            start_date = _clean(cell("start_date").get_text(" ", strip=True))

        contract = ""
        if cell("contract") is not None:
            contract = _clean(cell("contract").get_text(" ", strip=True))

        description_parts = []
        for label, value in (
            ("Institut", institution),
            ("Entgelt/Besoldung", pay_grade),
            ("Arbeitszeit", workload),
            ("Bewerbungsfrist", deadline),
            ("Besetzungsdatum", start_date),
            ("Befristung", contract),
        ):
            if value and value != DEFAULT_INSTITUTION:
                description_parts.append("%s: %s" % (label, value))
        description = row_text or " | ".join(description_parts)
        description = _clean(description)[:800]

        records.append({
            "title": title,
            "link": link,
            "institution": institution,
            "pay_grade": pay_grade,
            "workload": workload,
            "deadline": deadline,
            "description": description,
        })

    return records


# --------------------------------------------------------------------------
# Fallback: scan for individual postings / PDFs
# --------------------------------------------------------------------------

def _records_from_anchors(root, base_url: str) -> List[Dict[str, str]]:
    records: List[Dict[str, str]] = []
    seen_links = set()

    for anchor in root.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if _is_blocked_href(href):
            continue
        if not _has_job_hint(href):
            continue

        link = urljoin(base_url, href)
        if link in seen_links:
            continue

        title = _clean(anchor.get_text(" ", strip=True))
        if _is_nav_text(title) or title.lower().endswith(".pdf"):
            # PDF links often carry no readable label: borrow surrounding text.
            parent = anchor.find_parent(["li", "tr", "p", "div", "section", "article"])
            context = _clean(parent.get_text(" ", strip=True)) if parent is not None else ""
            if context and not _is_nav_text(context[:120]):
                title = context.split("  ")[0][:160]
            elif title.lower().endswith(".pdf"):
                title = _clean(link.rsplit("/", 1)[-1].replace("_", " "))
        if _is_nav_text(title):
            continue

        parent = anchor.find_parent(["li", "tr", "p", "div", "section", "article"])
        context = _clean(parent.get_text(" ", strip=True)) if parent is not None else _clean(title)

        seen_links.add(link)
        records.append({
            "title": title,
            "link": link,
            "institution": DEFAULT_INSTITUTION,
            "pay_grade": _find_pay_grade(context),
            "workload": _find_workload(context),
            "deadline": _norm_date(context),
            "description": context[:800],
        })

    return records


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def extract(html: str, base_url: str) -> list[dict]:
    """Extract individual job postings from a university vacancy page.

    Returns a list of dicts with the keys:
    ``title``, ``link``, ``institution``, ``pay_grade``, ``workload``,
    ``deadline``, ``description``.
    """
    results: List[Dict[str, str]] = []
    if not html or not isinstance(html, str):
        return results
    if not base_url:
        base_url = ""

    soup = BeautifulSoup(html, "html.parser")

    # Strip chrome that can never contain a single job posting.
    for tag in soup.find_all(["script", "style", "noscript", "nav", "footer",
                              "aside", "header", "form"]):
        tag.decompose()

    root = _content_root(soup)

    seen = set()

    # 1) Structured vacancy table(s).
    for table in _candidate_tables(root):
        for record in _rows_from_table(table, base_url):
            key = (record["title"].lower(), record["link"])
            if key in seen:
                continue
            seen.add(key)
            results.append(record)

    # 2) Fallback: individual posting links / job PDFs.
    if not results:
        for record in _records_from_anchors(root, base_url):
            key = (record["title"].lower(), record["link"])
            if key in seen:
                continue
            seen.add(key)
            results.append(record)

    return results
