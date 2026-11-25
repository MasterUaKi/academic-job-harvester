# File: skills_registry/job_skill_karriere_www_zeit_verlagsgruppe_de.py
# Version: 0.6.0 | Release Date: 2026-11-24
# Source: Karriere (www.zeit-verlagsgruppe.de)
# Auto-crystallized and AST-verified by PyCognition SkillCrystallizer.

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup


_WS_RE = re.compile(r"\s+")

# Only real job-detail URLs (single posting) are accepted.
_JOB_PATH_RE = re.compile(
    r"/(?:"
    r"job-ads|job-ad|jobs|job|"
    r"stellenangebote|stellenangebot|"
    r"stellenanzeigen|stellenanzeige|"
    r"stellenausschreibungen|stellenausschreibung|"
    r"stellen|"
    r"vacancies|vacancy|"
    r"careers/jobs|career/jobs"
    r")/([^/]+?)/?$",
    re.I,
)

# Navigation / legal / account pages must never become a posting.
_REJECT_PATH_RE = re.compile(
    r"(impressum|imprint|datenschutz|privacy|agb|cookies?|kontakt|contact|"
    r"login|registrier|merkliste|wishlist|newsletter|faq|sitemap|suche|search|"
    r"bewerbungsprozess)",
    re.I,
)

_NAV_TEXTS_RE = re.compile(
    r"^(?:"
    r"alle\s+jobs(?:\s+anzeigen)?|alle\s+stellenangebote|jobs(?:&nbsp;)?anzeigen|"
    r"mehr(?:\s+erfahren)?|details(?:\s+anzeigen)?|zur\s+merkliste|merkliste|"
    r"zurück|weiter|jobagent|startseite|karriere|bewerben(?:\s+als)?|hier\s+bewerben"
    r")$",
    re.I,
)

_DATE_PART = (
    r"(?:\d{1,2}[./]\d{1,2}[./]\d{2,4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}\.\s*[A-Za-zÄÖÜäöüß]+\s+\d{4})"
)

_DEADLINE_RE = re.compile(
    r"(?:bewerbungsfrist|bewerbungsschluss|bewerbungsende|einsendeschluss|"
    r"deadline|bewerbungen?\s+bis|kennziffer[^.]{0,40}?bis|application\s+deadline)"
    r"\s*[:\-–]?\s*(" + _DATE_PART + r")",
    re.I,
)

_PAY_RE = re.compile(
    r"(?:TV-?[LH]\s*[A-Za-z]?\s*\d{1,2}"
    r"|Entgeltgruppe\s*\d{1,2}"
    r"|EG\s*\d{1,2}"
    r"|E\s*\d{1,2}\s*(?:TV-?[LH]|TVöD)"
    r"|Besoldungsgruppe\s*[A-Za-z]?\s*\d{1,2}"
    r"|Vergütung\s+nach\s+[A-Za-zÄÖÜäöüß0-9\- ]{2,40})",
    re.I,
)

_WORKLOAD_RE = re.compile(
    r"(?:"
    r"\d{1,3}(?:[.,]\d)?\s*%\s*(?:der\s+|einer\s+)?"
    r"(?:Regelarbeitszeit|Stellenumfang|Vollzeitstelle|Wochenarbeitszeit)?"
    r"|(?:Vollzeit|Teilzeit)(?:\s*(?:/|und|oder)\s*(?:Vollzeit|Teilzeit))?"
    r"|\d{1,3}\s*(?:Stunden|Std\.?)\s*(?:/|pro|je)\s*Woche"
    r")",
    re.I,
)


def _clean(value: str) -> str:
    """Collapse whitespace and trim."""
    if not value:
        return ""
    return _WS_RE.sub(" ", value).strip()


def _is_job_url(url: str) -> bool:
    """True only for individual job detail pages (or job PDFs)."""
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme and parsed.scheme not in ("http", "https"):
        return False
    path = parsed.path or ""
    if not path or path == "/":
        return False
    low = path.lower()
    if _REJECT_PATH_RE.search(low):
        return False
    if low.endswith(".pdf"):
        return True
    if not _JOB_PATH_RE.search(low):
        return False
    # Reject bare listing/overview pages such as "/jobs/" or "/jobs".
    if low.rstrip("/").endswith(
        ("/jobs", "/job", "/stellen", "/stellenangebote", "/careers", "/karriere")
    ):
        return False
    return True


def _anchor_href(anchor) -> str:
    href = (anchor.get("href") or "").strip()
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return ""
    return href


def _find_link(node, base_url: str):
    """Locate the anchor that points to the individual posting."""
    title_el = node.find(["h3", "h2", "h4", "h5"])

    # 1) Heading wrapped in its own anchor.
    if title_el is not None:
        parent = title_el.find_parent("a", href=True)
        if parent is not None:
            href = _anchor_href(parent)
            if href and _is_job_url(urljoin(base_url, href)):
                return parent, title_el

    # 2) Anchor whose text matches the heading.
    title_text = _clean(title_el.get_text(" ")).lower() if title_el is not None else ""
    fallback = None
    for anchor in node.find_all("a", href=True):
        href = _anchor_href(anchor)
        if not href:
            continue
        link = urljoin(base_url, href)
        if not _is_job_url(link):
            continue
        anchor_text = _clean(anchor.get_text(" ")).lower()
        if title_text and title_text in anchor_text:
            return anchor, title_el
        if fallback is None:
            fallback = anchor

    if fallback is not None:
        return fallback, title_el
    return None, title_el


def _institution(node) -> str:
    for selector in (
        ".experience",
        ".institution",
        ".company",
        ".employer",
        ".organisation",
        ".organization",
        "[class*='institution']",
        "[class*='company']",
    ):
        el = node.select_one(selector)
        if el is not None:
            text = _clean(el.get_text(" "))
            if text:
                return text
    return ""


def _description(node) -> str:
    parts = []

    pre = node.select_one(".preinfo") or node.select_one(".job-meta")
    if pre is not None:
        for selector in (
            ".category",
            ".city",
            ".remote",
            ".jobtype",
            ".employment-type",
            ".level",
        ):
            el = pre.select_one(selector)
            if el is None:
                continue
            value = _clean(el.get_text(" ")).strip(" /|-")
            if value and value not in parts:
                parts.append(value)

    for el in node.find_all(["p", "li"]):
        text = _clean(el.get_text(" "))
        if len(text) > 3 and text not in parts:
            parts.append(text)

    return _clean(" | ".join(parts))


def _extract_from_node(node, base_url: str):
    anchor, title_el = _find_link(node, base_url)
    if anchor is None:
        return None

    href = _anchor_href(anchor)
    if not href:
        return None

    link = urljoin(base_url, href)
    if not _is_job_url(link):
        return None

    if title_el is not None:
        title = _clean(title_el.get_text(" "))
    else:
        title = _clean(anchor.get_text(" "))

    if not title or _NAV_TEXTS_RE.match(title):
        return None
    if len(title) < 4:
        return None

    node_text = _clean(node.get_text(" "))

    pay_grade = ""
    pay_match = _PAY_RE.search(node_text)
    if pay_match:
        pay_grade = _clean(pay_match.group(0))

    workload = ""
    workload_match = _WORKLOAD_RE.search(node_text)
    if workload_match:
        workload = _clean(workload_match.group(0))

    deadline = ""
    deadline_match = _DEADLINE_RE.search(node_text)
    if deadline_match:
        deadline = _clean(deadline_match.group(1))

    description = _description(node)
    if not description:
        description = title

    return {
        "title": title,
        "link": link,
        "institution": _institution(node),
        "pay_grade": pay_grade,
        "workload": workload,
        "deadline": deadline,
        "description": description,
    }


def extract(html: str, base_url: str) -> list[dict]:
    """Extract individual job postings from a career listing page."""
    if not html or not isinstance(html, str):
        return []

    soup = BeautifulSoup(html, "html.parser")
    results: list[dict] = []
    seen = set()

    containers = soup.select("div.job")
    if not containers:
        containers = soup.select(
            "article.job, li.job, div.job-item, div.job-item-wrapper, "
            "div.stellenangebot, div.stellenanzeige, div.job-listing, "
            "div.vacancy, div.joboffer, li.stellenangebot"
        )

    for node in containers:
        record = _extract_from_node(node, base_url)
        if record is None:
            continue
        if record["link"] in seen:
            continue
        seen.add(record["link"])
        results.append(record)

    # Deterministic fallback: harvest anchors that clearly point to a posting.
    if not results:
        for anchor in soup.find_all("a", href=True):
            href = _anchor_href(anchor)
            if not href:
                continue
            link = urljoin(base_url, href)
            if not _is_job_url(link):
                continue
            if link in seen:
                continue
            title = _clean(anchor.get_text(" "))
            if not title or len(title) < 4 or _NAV_TEXTS_RE.match(title):
                continue
            seen.add(link)
            results.append(
                {
                    "title": title,
                    "link": link,
                    "institution": "",
                    "pay_grade": "",
                    "workload": "",
                    "deadline": "",
                    "description": title,
                }
            )

    return results
