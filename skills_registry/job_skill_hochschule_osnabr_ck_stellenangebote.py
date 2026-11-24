# File: skills_registry/job_skill_hochschule_osnabr_ck_stellenangebote.py
# Version: 0.4.1 | Release Date: 2026-11-24
# Source: Hochschule Osnabrück Stellenangebote
# Auto-crystallized and AST-verified by PyCognition SkillCrystallizer.

"""Deterministic extractor for academic / university job postings.

The extractor expects the HTML of a job listing page (or of a single job detail
page) and returns one dict per individual posting. Navigation, breadcrumb,
imprint, privacy and generic overview links are rejected.
"""

import re
from datetime import date
from urllib.parse import urljoin, urlparse, unquote

from bs4 import BeautifulSoup
from bs4.element import Tag


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

_JOB_KEYWORD_RE = re.compile(
    r"(stellenausschreib\w*|stellenangebot\w*|stellenanzeige\w*|"
    r"job|jobs|karriere|career|vacanc\w*|bewerbung\w*|ausschreibung\w*|"
    r"professur\w*|lehrstuhl\w*|wissenschaftliche[rnm]?\s+mitarbeiter\w*|"
    r"mitarbeiter\w*|referent\w*|position\w*)",
    re.I,
)

_SECTION_RE = re.compile(
    r"(stellenangebot|stellenausschreib|stellenanzeig|job|karriere|career|vacanc|bewerb)",
    re.I,
)

_SECTION_SEGMENTS = {
    "stellenangebote", "stellenangebot", "stellenausschreibungen",
    "stellenausschreibung", "stellenanzeigen", "stellenanzeige",
    "jobs", "job", "karriere", "career", "careers", "bewerbung",
    "ausschreibungen", "vacancies", "vacancy", "offene-stellen",
    "stellenmarkt", "jobboerse", "jobbörse",
}

_PDF_RE = re.compile(r"\.pdf($|\?|/)", re.I)

_EXCLUDED_PATHS = (
    "/impressum", "/imprint", "/datenschutz", "/datenschutzerklaerung",
    "/privacy", "/kontakt", "/contact", "/sitemap", "/barrierefreiheit",
    "/accessibility", "/agb", "/cookies", "/login", "/logout", "/presse",
    "/newsletter", "/faq",
)

_NAV_LABELS = {
    "impressum", "imprint", "datenschutz", "datenschutzerklärung",
    "datenschutzerklaerung", "privacy", "privacy policy", "kontakt",
    "contact", "startseite", "home", "zur startseite", "sitemap",
    "barrierefreiheit", "accessibility", "login", "logout", "anmelden",
    "bewerbungsportal", "faq", "presse", "newsletter", "cookies",
    "zum inhalt", "zur navigation", "skip to content", "agb",
    "stellenangebote", "jobs und karriere", "karriere", "jobs",
    "stellenausschreibungen", "stellenmarkt", "offene stellen",
    "übersicht", "zur übersicht", "alle stellenangebote",
}

_GENERIC_LINK_TEXT = {
    "mehr", "mehr erfahren", "weiterlesen", "weiter lesen", "details",
    "detail", "zum stellenangebot", "zur stellenausschreibung",
    "zur stelle", "stellenangebot", "stellenausschreibung", "hier",
    "hier klicken", "klicken sie hier", "öffnen", "oeffnen", "anzeigen",
    "pdf", "download", "bewerben", "jetzt bewerben", "link",
    "zur anzeige", "zur ausschreibung", "vollständige anzeige",
    "informationen", "read more",
}

_GENERIC_TITLES = {
    "stellenangebote", "stellenangebot", "stellenausschreibungen",
    "stellenanzeigen", "jobs", "job", "karriere", "career",
    "jobs und karriere", "offene stellen", "aktuelle stellenangebote",
    "stellenmarkt", "übersicht", "bewerbung", "home", "startseite",
}

_NOISE_TAGS = [
    "nav", "footer", "aside", "script", "style", "form", "noscript",
    "svg", "iframe", "button",
]

_NOISE_ATTR_RE = re.compile(
    r"(breadcrumb|navigation|subnav|navbar|menu|sidebar|site-?header|"
    r"page-?header|skip-?link|social|pagination|pager|cookie|toolbar)",
    re.I,
)

_INST_RE = re.compile(
    r"\b((?:Hochschule|Universität|Universitaet|Fachhochschule|"
    r"Technische\s+Universität|TU)\s+"
    r"[A-ZÄÖÜ][\w\-äöüß\.]*"
    r"(?:\s+[A-ZÄÖÜ][\w\-äöüß\.]*){0,3})"
)

_PAY_PATTERNS = (
    (re.compile(r"TV-?L\s*(?:[-–]\s*)?(?:E\s*)?(\d{1,2})", re.I), "TV-L E {}"),
    (re.compile(r"TV-?[ÖO]D\s*(?:[-–]\s*)?(?:E\s*)?(\d{1,2})", re.I), "TVöD E {}"),
    (re.compile(r"Entgeltgruppe\s*(\d{1,2})", re.I), "Entgeltgruppe {}"),
    (re.compile(r"Besoldungsgruppe\s*([AW]\s?\d{1,2})", re.I), "Besoldung {}"),
    (re.compile(r"\b(W\s?[23])\b"), "{}"),
)

_PERCENT_RE = re.compile(r"(\d{1,3}(?:[.,]\d)?)\s*(?:%|Prozent|v\.\s?H\.)", re.I)
_HOURS_RE = re.compile(
    r"(\d{1,2}(?:[.,]\d)?)\s*(?:Wochenstunden|Stunden\s*(?:pro|/)\s*Woche|"
    r"Std\.?\s*(?:pro|/)\s*Woche)",
    re.I,
)
_DEADLINE_RE = re.compile(
    r"(?:bewerbungs(?:frist|schluss|ende)|bewerbungen\s+bis|"
    r"bewerbungszeitraum\s+bis|frist|deadline)"
    r"\D{0,20}(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})",
    re.I,
)
_DATE_RE = re.compile(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _clean(value: str) -> str:
    """Collapse whitespace and strip control characters."""
    return re.sub(r"\s+", " ", (value or "")).strip()


def _normalize_url(url: str) -> str:
    """Normalised comparison key for a URL (no fragment, no trailing slash)."""
    url = (url or "").split("#", 1)[0].strip()
    return url.rstrip("/").lower()


def _strip_noise(soup: BeautifulSoup) -> None:
    """Remove navigation, header/footer chrome and other non-content areas."""
    for tag in soup.find_all(_NOISE_TAGS):
        try:
            tag.decompose()
        except Exception:
            pass
    for tag in soup.find_all(attrs={"class": _NOISE_ATTR_RE}):
        try:
            tag.decompose()
        except Exception:
            pass
    for tag in soup.find_all(attrs={"id": _NOISE_ATTR_RE}):
        try:
            tag.decompose()
        except Exception:
            pass


def _is_excluded_path(path: str) -> bool:
    if not path or path == "/":
        return True
    for prefix in _EXCLUDED_PATHS:
        if path.startswith(prefix):
            return True
    return False


def _looks_like_detail(path: str) -> bool:
    """True when a URL points at an individual posting (detail page or PDF)."""
    segments = [seg for seg in path.split("/") if seg]
    if not segments:
        return False
    last = segments[-1]
    if last in _SECTION_SEGMENTS:
        return False
    if _PDF_RE.search(last):
        return True
    for seg in segments[:-1]:
        if seg in _SECTION_SEGMENTS or _SECTION_RE.search(seg):
            return len(last) > 2
    if _JOB_KEYWORD_RE.search(last) and re.search(r"\d", last):
        return True
    return False


def _is_nav_label(text: str) -> bool:
    low = text.lower()
    if not low or len(low) > 250:
        return True
    if low in _NAV_LABELS:
        return True
    return False


def _block_for(anchor: Tag) -> Tag:
    """Find the smallest enclosing block that represents one list entry."""
    node = anchor
    for _ in range(5):
        parent = node.parent
        if parent is None or not isinstance(parent, Tag):
            break
        node = parent
        if node.name in ("li", "article", "section", "tr", "dd", "figure"):
            return node
        classes = " ".join(node.get("class") or []).lower()
        if any(key in classes for key in
               ("teaser", "card", "list-item", "item", "box", "entry", "result")):
            return node
    parent = anchor.parent
    return parent if isinstance(parent, Tag) else anchor


def _resolve_title(anchor: Tag, block: Tag, text: str) -> str:
    low = text.lower()
    if text and low not in _GENERIC_LINK_TEXT and 5 <= len(text) <= 200:
        return text
    if block is not None:
        for name in ("h1", "h2", "h3", "h4", "h5", "h6", "strong", "b"):
            heading = block.find(name)
            if heading is None:
                continue
            candidate = _clean(heading.get_text(" "))
            if candidate and len(candidate) >= 5 and candidate.lower() not in _NAV_LABELS:
                return candidate[:200]
    if text and len(text) >= 5 and low not in _GENERIC_LINK_TEXT:
        return text[:200]
    return ""


def _resolve_description(block: Tag, title: str) -> str:
    if block is None:
        return ""
    parts = []
    for paragraph in block.find_all(["p", "span", "div"]):
        value = _clean(paragraph.get_text(" "))
        if not value or value == title or len(value) < 25:
            continue
        if value in parts:
            continue
        parts.append(value)
        if sum(len(p) for p in parts) > 400:
            break
    if not parts:
        whole = _clean(block.get_text(" "))
        if title and whole.startswith(title):
            whole = whole[len(title):].strip()
        return whole[:500]
    return " ".join(parts)[:500]


def _extract_pay_grade(text: str) -> str:
    if not text:
        return ""
    for pattern, template in _PAY_PATTERNS:
        match = pattern.search(text)
        if match:
            return template.format(_clean(match.group(1)))
    if re.search(r"besoldung", text, re.I):
        match = re.search(r"\b(A\s?1[0-6])\b", text)
        if match:
            return "Besoldung " + _clean(match.group(1))
    return ""


def _extract_workload(text: str) -> str:
    if not text:
        return ""
    match = _PERCENT_RE.search(text)
    if match:
        return _clean(match.group(1)).replace(",", ".") + " %"
    match = _HOURS_RE.search(text)
    if match:
        return _clean(match.group(0))
    match = re.search(r"\b(Vollzeit|Teilzeit)\b", text, re.I)
    if match:
        return match.group(1).capitalize()
    return ""


def _extract_deadline(text: str) -> str:
    if not text:
        return ""
    match = _DEADLINE_RE.search(text)
    if match is None:
        if not re.search(r"bewerb", text, re.I):
            return ""
        match = _DATE_RE.search(text)
        if match is None:
            return ""
    day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if year < 100:
        year += 2000
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return "{:02d}.{:02d}.{:04d}".format(day, month, year)


def _extract_institution(text: str, default: str) -> str:
    if text:
        match = _INST_RE.search(text)
        if match:
            name = _clean(match.group(1))
            if 3 < len(name) <= 80:
                return name
    return default


def _default_institution(soup: BeautifulSoup, base_url: str) -> str:
    host = (urlparse(base_url or "").netloc or "").lower()
    known = {"hs-osnabrueck.de": "Hochschule Osnabrück"}
    for domain, name in known.items():
        if host.endswith(domain):
            return name
    heading = soup.find("h1")
    if heading is not None:
        value = _clean(heading.get_text(" "))
        if value and value.lower() not in _GENERIC_TITLES and 3 <= len(value) <= 80:
            return value
    return host or "Hochschule"


def _extract_single_detail(soup: BeautifulSoup, base_url: str,
                           default_institution: str) -> dict:
    """Fallback: the page itself is one job detail page."""
    heading = soup.find("h1")
    if heading is None:
        return None
    title = _clean(heading.get_text(" "))
    if not title or title.lower() in _GENERIC_TITLES:
        return None
    if len(title.split()) < 3 or not _JOB_KEYWORD_RE.search(title):
        return None
    root = soup.find("main") or soup.body or soup
    body = _clean(root.get_text(" "))
    if not re.search(r"(bewerbungsfrist|bewerbungsschluss|entgeltgruppe|"
                     r"tv-?l|besoldung|\bW\s?[23]\b)", body, re.I):
        return None
    return {
        "title": title[:200],
        "link": base_url,
        "institution": _extract_institution(body, default_institution),
        "pay_grade": _extract_pay_grade(body),
        "workload": _extract_workload(body),
        "deadline": _extract_deadline(body),
        "description": body[:500],
    }


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def extract(html: str, base_url: str) -> list[dict]:
    """Extract individual academic job postings from a listing/detail page."""
    soup = BeautifulSoup(html or "", "html.parser")
    _strip_noise(soup)

    default_institution = _default_institution(soup, base_url)
    base_key = _normalize_url(base_url)

    results: list[dict] = []
    seen: set = set()

    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue

        link = urljoin(base_url, href).split("#", 1)[0].strip()
        if not link or _normalize_url(link) == base_key:
            continue

        parsed = urlparse(link)
        if parsed.scheme not in ("http", "https"):
            continue

        path = parsed.path.lower()
        if _is_excluded_path(path):
            continue
        if not _looks_like_detail(path):
            continue

        text = _clean(anchor.get_text(" "))
        if _is_nav_label(text):
            continue

        block = _block_for(anchor)
        context = _clean(block.get_text(" ")) if block is not None else ""
        blob = " ".join((text, unquote(path), context[:400]))
        if not _JOB_KEYWORD_RE.search(blob):
            continue

        title = _resolve_title(anchor, block, text)
        if not title or title.lower() in _GENERIC_TITLES:
            continue

        if link in seen:
            continue
        seen.add(link)

        results.append({
            "title": title,
            "link": link,
            "institution": _extract_institution(context or text, default_institution),
            "pay_grade": _extract_pay_grade(context or text),
            "workload": _extract_workload(context or text),
            "deadline": _extract_deadline(context or text),
            "description": _resolve_description(block, title),
        })

    if not results:
        single = _extract_single_detail(soup, base_url, default_institution)
        if single is not None:
            results.append(single)

    return results
