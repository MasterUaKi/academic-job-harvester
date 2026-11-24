# File: skills_registry/job_skill_tu_braunschweig_stellenmarkt.py
# Version: 0.4.1 | Release Date: 2026-11-24
# Source: TU Braunschweig Stellenmarkt
# Auto-crystallized and AST-verified by PyCognition SkillCrystallizer.

import re
from bs4 import BeautifulSoup
from urllib.parse import urljoin

INSTITUTION = "TU Braunschweig"

# Keywords that mark an anchor/heading as describing a real job posting.
_JOB_TITLE_HINTS = (
    "mitarbeiter", "mitarbeiterin", "wissenschaftl", "wiss.", "wiss ",
    "professur", "professor", "juniorprofessur", "lehrstuhl",
    "doktorand", "doktorandin", "postdoc", "post-doc",
    "ingenieur", "ingenieurin", "techniker", "technikerin",
    "assistent", "assistentin", "referent", "referentin",
    "koordinator", "koordinatorin", "sachbearbeit",
    "leiter", "leiterin", "leitung",
    "research", "phd", "trainee", "ausbildung", "auszubildende",
    "praktik", "stellenangebot", "stellenausschreibung",
)

# Slugs we must never accept (navigation, legal, general information).
_EXCLUDE_HREF = (
    r"/impressum", r"/datenschutz", r"/privacy", r"/sitemap",
    r"/kontakt", r"/barrierefreiheit", r"/leichte-sprache", r"/login",
    r"/studieninteressierte/", r"/bewerbung/?$", r"/bewerbung/#",
    r"/studium/", r"/studienangebot", r"/studiengänge",
    r"/forschung/", r"/transfer/", r"/international/",
    r"/alumni/", r"/career/service",
    r"^#", r"^javascript:", r"^mailto:", r"^tel:",
)

# Slugs that typically identify individual job detail pages.
_JOB_HREF_HINTS = (
    r"/stellenmarkt/(?!$)",
    r"/stellenangebot",
    r"/stellenausschreibung",
    r"/job/", r"/jobs/",
    r"[?&]jobid=",
)

# Generic section headings that should not be treated as job titles.
_GENERIC_TITLES = {
    "aktuelle stellenangebote", "stellenangebote", "stellenmarkt",
    "offene stellen", "offene stellenangebote", "jobs", "jobangebote",
    "ausschreibungen", "stellenausschreibungen", "alle stellenangebote",
    "karriere", "arbeiten an der tu braunschweig",
}

# Regexes for structured fields inside a posting block.
_RE_PAY_GRADE = re.compile(
    r"(?:entgeltgruppe|entgeltgr\.?|tv-l|tvöd|tv-öd|besoldungsgruppe|bes\.?\s*gr\.?)"
    r"\s*[:\-]?\s*"
    r"([a-zA-Z]?\s*\d{1,2}(?:\s*[-,]\s*\d{1,2})?[a-zA-Z]?)",
    re.IGNORECASE,
)
_RE_PAY_GRADE_SHORT = re.compile(
    r"\b(?:E\s?\d{1,2}|TV-L\s?\d{1,2}|A\s?\d{1,2})\b"
)
_RE_WORKLOAD_PCT = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s*(?:%|prozent)", re.IGNORECASE)
_RE_WORKLOAD_KIND = re.compile(r"\b(teilzeit|vollzeit)\b", re.IGNORECASE)
_RE_DEADLINE = re.compile(
    r"(?:bewerbungs(?:frist|schluss)|frist|bis(?:\s+zum)?)\D{0,25}"
    r"(\d{1,2}[./]\d{1,2}[./]\d{2,4}|\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
_RE_ANY_DATE = re.compile(r"\b(\d{1,2}[./]\d{1,2}[./]\d{2,4}|\d{4}-\d{2}-\d{2})\b")


def _clean(text):
    return re.sub(r"\s+", " ", (text or "")).strip()


def _has_job_hint(text):
    t = (text or "").lower()
    return any(k in t for k in _JOB_TITLE_HINTS)


def _looks_like_job_href(href):
    if not href:
        return False
    h = href.lower().strip()
    for pat in _EXCLUDE_HREF:
        if re.search(pat, h):
            return False
    if h.endswith(".pdf"):
        return True
    for pat in _JOB_HREF_HINTS:
        if re.search(pat, h):
            return True
    return False


def _find_block(anchor):
    """Walk up from an anchor until we hit the element that represents one posting."""
    node = anchor
    fallback = anchor.parent if anchor.parent is not None else anchor
    for _ in range(8):
        node = node.parent
        if node is None:
            break
        if node.name in ("li", "article"):
            return node
        classes = " ".join(node.get("class") or []).lower()
        if any(k in classes for k in ("job", "stelle", "teaser", "offer", "listing")):
            return node
    return fallback


def _extract_title(anchor, block):
    text = _clean(anchor.get_text(" ", strip=True))
    if len(text) >= 15 and _has_job_hint(text):
        return text

    if block is not None:
        for tag in block.find_all(["h2", "h3", "h4", "h5", "strong", "b"]):
            candidate = _clean(tag.get_text(" ", strip=True))
            if len(candidate) >= 15 and _has_job_hint(candidate):
                return candidate
        for inner in block.find_all("a", href=True):
            candidate = _clean(inner.get_text(" ", strip=True))
            if len(candidate) >= 15 and _has_job_hint(candidate):
                return candidate

    title_attr = _clean(anchor.get("title", ""))
    if title_attr and _has_job_hint(title_attr):
        return title_attr
    return text


def _valid_title(title):
    if not title or len(title) < 8:
        return False
    if title.lower().strip() in _GENERIC_TITLES:
        return False
    return _has_job_hint(title)


def _extract_description(block, title):
    if block is None:
        return ""
    parts = []
    for p in block.find_all(["p", "li", "dd", "div"]):
        text = _clean(p.get_text(" ", strip=True))
        if not text or len(text) < 12:
            continue
        if text == title:
            continue
        low = text.lower()
        if low.startswith(("weiterlesen", "mehr erfahren",
                           "zum stellenangebot", "jetzt bewerben",
                           "zur bewerbung", "alle anzeigen")):
            continue
        parts.append(text)

    seen = set()
    ordered = []
    for p in parts:
        if p in seen:
            continue
        seen.add(p)
        ordered.append(p)
    return " ".join(ordered)[:2000]


def _extract_pay_grade(text):
    if not text:
        return ""
    m = _RE_PAY_GRADE.search(text)
    if m:
        return _clean(m.group(0))
    m = _RE_PAY_GRADE_SHORT.search(text)
    if m:
        return _clean(m.group(0))
    return ""


def _extract_workload(text):
    if not text:
        return ""
    m = _RE_WORKLOAD_PCT.search(text)
    if m:
        return _clean(m.group(0))
    m = _RE_WORKLOAD_KIND.search(text)
    if m:
        return m.group(1).capitalize()
    return ""


def _extract_deadline(text):
    if not text:
        return ""
    m = _RE_DEADLINE.search(text)
    if m:
        return _clean(m.group(1))
    m = _RE_ANY_DATE.search(text)
    if m:
        return _clean(m.group(1))
    return ""


def extract(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html or "", "html.parser")

    # Strip global chrome so we only look at the content area.
    for tag in soup.find_all(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    for tag in soup.find_all(
        class_=re.compile(r"(main-nav|breadcrumb|footer|header__|no-index)", re.I)
    ):
        if tag.name in ("body", "html", "main"):
            continue
        tag.decompose()

    results = []
    seen_links = set()

    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if not _looks_like_job_href(href):
            continue

        abs_link = urljoin(base_url, href)
        if abs_link in seen_links:
            continue

        block = _find_block(anchor)
        title = _extract_title(anchor, block)
        if not _valid_title(title):
            continue

        seen_links.add(abs_link)
        block_text = _clean(block.get_text(" ", strip=True)) if block else ""

        results.append({
            "title": title,
            "link": abs_link,
            "institution": INSTITUTION,
            "pay_grade": _extract_pay_grade(block_text),
            "workload": _extract_workload(block_text),
            "deadline": _extract_deadline(block_text),
            "description": _extract_description(block, title),
        })

    return results
