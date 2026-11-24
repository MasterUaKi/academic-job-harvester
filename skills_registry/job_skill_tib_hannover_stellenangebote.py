# File: skills_registry/job_skill_tib_hannover_stellenangebote.py
# Version: 0.4.1 | Release Date: 2026-11-24
# Source: TIB Hannover Stellenangebote
# Auto-crystallized and AST-verified by PyCognition SkillCrystallizer.

import re
from typing import Optional
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup


DEFAULT_INSTITUTION = "TIB – Leibniz-Informationszentrum Technik und Naturwissenschaften"

_GENERIC_LINK_TEXT = re.compile(
    r"^(?:mehr(?:\s+erfahren)?|weiterlesen|weiter|details?|hier|zur\s+stelle|zur\s+ausschnung|"
    r"zur\s+ausschreibung|zum\s+stellenangebot|öffnen|oeffnen|pdf|download|bewerben|"
    r"jetzt\s+bewerben|link|>>>?|>>?)\s*$",
    re.I,
)

_BLOCKED_TERMS = (
    "impressum", "imprint", "datenschutz", "privacy", "cookie", "cookies",
    "agb", "sitemap", "barrierefrei", "accessibility", "kontakt", "contact",
    "login", "anmelden", "abmelden", "newsletter", "presse", "press",
    "startseite", "zurück", "back to top", "über uns", "about us",
    "leichte sprache", "gebärdensprache", "gebaerdensprache",
    "facebook", "instagram", "linkedin", "youtube", "xing", "mastodon",
    "twitter", "rss", "social media", "weitere informationen",
    "alle stellenangebote", "stellenangebote im überblick",
)

_BLOCKED_HOSTS = (
    "facebook.com", "instagram.com", "linkedin.com", "youtube.com",
    "twitter.com", "x.com", "xing.com", "mastodon", "wikipedia.org",
    "google.com", "apple.com", "play.google.com",
)

_BLOCKED_PATH_SEGMENTS = (
    "/impressum", "/imprint", "/datenschutz", "/privacy", "/cookie",
    "/agb", "/sitemap", "/barrierefrei", "/accessibility", "/login",
    "/newsletter", "/presse", "/press", "/kontakt", "/contact",
    "/mastodon", "/facebook", "/instagram", "/linkedin", "/youtube",
)

_BAD_FILE_EXT = re.compile(
    r"\.(?:jpe?g|png|gif|svg|webp|ico|bmp|tiff?|css|js|json|xml|zip|gz|mp4|mp3|woff2?|ttf)$",
    re.I,
)

_INDEX_PATH_RE = re.compile(
    r"/(?:aktuelle[-_])?stellenangebote?/?$"
    r"|/karriere(?:-und-ausbildung)?/?$"
    r"|/jobs?/?$"
    r"|/ausbildung/?$",
    re.I,
)

_URL_JOB_HINT = re.compile(
    r"(stellenangebot|stellenausschreibung|stellenanzeige|/stellen/|/stelle-|"
    r"/jobs?/|/job-|karriere[^?#]*detail|bewerbung|"
    r"/pdf/|\.pdf(?:$|\?))",
    re.I,
)

_TITLE_JOB_HINT = re.compile(
    r"(m/w/d|m/w/x|w/m/d|d/m/w|m/w/f|\(gn\*?\)|/gn\*?)"
    r"|stellenausschreibung|stellenangebot|stellenanzeige"
    r"|wissenschaftler|wissenschaftliche|mitarbeiter|mitarbeit|referent|referentin"
    r"|ingenieur|entwickler|informatiker|bibliothekar|archivar|doktorand|postdoc"
    r"|praktikant|praktikum|auszubildende|azubi|trainee|volontär|volontariat"
    r"|leitung|leiter|koordinator|sachbearbeit|assistenz|spezialist|expert"
    r"|software|data|research|researcher|engineer|curator|kurator|manager"
    r"|studentische hilfskraft|hilfskraft|berater",
    re.I,
)

_CONTAINER_HINT = re.compile(
    r"(job|stelle|stellen|position|vacanc|teaser|card|list-?item|item|entry|angebot|result)",
    re.I,
)

_MONTH_NAMES = (
    r"(?:Januar|Jänner|Jaenner|Februar|März|Maerz|April|Mai|Juni|Juli|"
    r"August|September|Oktober|November|Dezember)"
)
_MONTH_NAME_RE = re.compile(_MONTH_NAMES, re.I)

_DEADLINE_LABEL = (
    r"(?:bewerbungs(?:frist|schluss|ende|zeitraum)|bewerbungsschluss|"
    r"bewerben sie sich (?:bis|spätestens)|einsendeschluss|"
    r"kennziffer[^0-9]{0,25}|deadline|closing date|application deadline)"
)

_DEADLINE_PATTERNS = (
    re.compile(
        _DEADLINE_LABEL + r"[^0-9]{0,40}(\d{1,2}\s?[./]\s?\d{1,2}\s?[./]\s?\d{2,4})",
        re.I,
    ),
    re.compile(
        _DEADLINE_LABEL + r"[^\n]{0,40}?(\d{1,2}\.?\s?" + _MONTH_NAMES + r"\s?\d{4})",
        re.I,
    ),
)

_ANY_DATE_RE = re.compile(r"\b(\d{1,2}\s?[./]\s?\d{1,2}\s?[./]\s?\d{4})\b")

_PAY_PATTERNS = (
    re.compile(
        r"((?:TV-?L|TV-?öD|TVöD|TV-?L-S)[^\d\n]{0,12}\d{1,2}(?:\s*(?:-|bis)\s*\d{1,2})?)",
        re.I,
    ),
    re.compile(
        r"((?:Entgeltgruppe|Eingruppierung|Vergütungsgruppe|Besoldungsgruppe|"
        r"Vergütung|Bezahlung)[^\d\n]{0,20}\d{1,2}(?:\s*(?:-|bis)\s*\d{1,2})?)",
        re.I,
    ),
    re.compile(r"\b(E\s?-?\s?\d{1,2})\b"),
)

_WORKLOAD_PATTERNS = (
    re.compile(r"(\d{1,3}(?:[,.]\d{1,2})?\s*(?:%|Prozent))", re.I),
    re.compile(
        r"(\d{1,2}(?:[,.]\d{1,2})?\s*(?:Wochenstunden|Stunden\s*(?:pro|/)\s*Woche|"
        r"Std\.?\s*(?:pro|/)\s*Woche))",
        re.I,
    ),
    re.compile(r"\b(Vollzeit|Teilzeit)\b", re.I),
)


def _clean(value: Optional[str]) -> str:
    if not value:
        return ""
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _is_challenge_page(soup: BeautifulSoup, page_text: str) -> bool:
    haystack = page_text.lower()[:6000]
    title_tag = soup.find("title")
    if title_tag is not None:
        haystack = _clean(title_tag.get_text(" ")).lower() + " " + haystack
    if soup.find(id="anubis-script-error") is not None:
        return True
    if "anubis" in haystack and ("proof of work" in haystack or "not a bot" in haystack):
        return True
    if "making sure you're not a bot" in haystack:
        return True
    if "just a moment" in haystack and "cloudflare" in haystack:
        return True
    if "checking your browser" in haystack:
        return True
    return False


def _detect_institution(soup: BeautifulSoup, page_text: str) -> str:
    meta = soup.find("meta", attrs={"property": "og:site_name"})
    if meta is not None:
        content = _clean(meta.get("content") or "")
        if 3 <= len(content) <= 160:
            return content
    low = page_text.lower()
    if "tib" in low and "leibniz" in low:
        return DEFAULT_INSTITUTION
    return DEFAULT_INSTITUTION


def _find_container(anchor) -> Optional[object]:
    node = anchor
    for _ in range(5):
        parent = getattr(node, "parent", None)
        if parent is None:
            break
        name = getattr(parent, "name", None)
        if name in (None, "[document]", "html", "body", "main", "nav", "header", "footer"):
            break
        classes = parent.get("class") or []
        ident = " ".join(classes) + " " + (parent.get("id") or "")
        if _CONTAINER_HINT.search(ident):
            return parent
        node = parent
    return getattr(anchor, "parent", None)


def _title_from_container(container) -> str:
    if container is None:
        return ""
    try:
        headings = container.find_all(("h1", "h2", "h3", "h4", "h5", "h6", "strong", "b"), limit=8)
    except Exception:
        return ""
    for tag in headings:
        text = _clean(tag.get_text(" "))
        if 6 <= len(text) <= 200:
            return text
    return ""


def _normalize_date(value: str) -> str:
    value = _clean(value)
    if _MONTH_NAME_RE.search(value):
        return value[:40]
    value = value.replace("/", ".").replace("-", ".")
    value = re.sub(r"\s*\.\s*", ".", value)
    value = re.sub(r"\.{2,}", ".", value)
    match = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{2,4})$", value)
    if match:
        day, month, year = match.groups()
        if len(year) == 2:
            year = "20" + year
        return "%02d.%02d.%s" % (int(day), int(month), year)
    return value


def _find_deadline(text: str) -> str:
    if not text:
        return ""
    for pattern in _DEADLINE_PATTERNS:
        match = pattern.search(text)
        if match:
            return _normalize_date(match.group(1))
    if "bewerb" in text.lower():
        match = _ANY_DATE_RE.search(text)
        if match:
            return _normalize_date(match.group(1))
    return ""


def _find_pay_grade(text: str) -> str:
    if not text:
        return ""
    for pattern in _PAY_PATTERNS:
        match = pattern.search(text)
        if match:
            return _clean(match.group(1))[:60]
    return ""


def _find_workload(text: str) -> str:
    if not text:
        return ""
    for pattern in _WORKLOAD_PATTERNS:
        match = pattern.search(text)
        if match:
            return _clean(match.group(1))[:60]
    return ""


def _build_description(context: str, title: str, limit: int = 600) -> str:
    text = _clean(context)
    if title and text.lower().startswith(title.lower()):
        text = text[len(title):]
    text = text.strip(" -–—|·•:,;")
    text = re.sub(r"^(?:mehr erfahren|weiterlesen|details?)\b[:\s]*", "", text, flags=re.I)
    return text[:limit].strip()


def _is_relevant_job(title: str, link: str, base_url: str) -> bool:
    if not title or len(title) < 6 or len(title) > 300:
        return False

    low_title = title.lower()
    for term in _BLOCKED_TERMS:
        if term in low_title:
            return False

    try:
        parsed = urlparse(link)
    except Exception:
        return False

    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()

    for bad_host in _BLOCKED_HOSTS:
        if bad_host in host:
            return False

    for segment in _BLOCKED_PATH_SEGMENTS:
        if segment in path:
            return False

    if _BAD_FILE_EXT.search(path):
        return False

    if _INDEX_PATH_RE.search(path.rstrip("/") + "/") and not path.endswith(".pdf"):
        normalized = path.rstrip("/")
        if not re.search(r"(detail|anzeige|ausschreibung|/id[-/]|\d{3,})", normalized):
            return False

    if base_url:
        try:
            base_path = (urlparse(base_url).path or "").lower().rstrip("/")
        except Exception:
            base_path = ""
        if base_path and base_path != "/" and path.rstrip("/") == base_path:
            return False

    has_url_hint = bool(_URL_JOB_HINT.search(link))
    has_title_hint = bool(_TITLE_JOB_HINT.search(title))

    if not (has_url_hint or has_title_hint):
        return False

    return True


def extract(html: str, base_url: str) -> list[dict]:
    if not isinstance(html, str) or not html.strip():
        return []
    if not isinstance(base_url, str):
        base_url = ""

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []

    for tag in soup.find_all(["script", "style", "noscript", "template"]):
        tag.decompose()

    page_text = _clean(soup.get_text(" "))

    if _is_challenge_page(soup, page_text):
        return []

    institution = _detect_institution(soup, page_text)

    results: list[dict] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        raw_href = (anchor.get("href") or "").strip()
        if not raw_href:
            continue

        low_href = raw_href.lower()
        if low_href.startswith(("#", "javascript:", "mailto:", "tel:", "ftp:", "data:")):
            continue

        try:
            link = urljoin(base_url, raw_href)
        except Exception:
            continue

        if not link.lower().startswith(("http://", "https://")):
            continue

        link = link.split("#")[0].strip()
        if not link:
            continue

        key = link.rstrip("/").lower()
        if key in seen:
            continue

        container = _find_container(anchor)

        title = _clean(anchor.get_text(" "))
        if _GENERIC_LINK_TEXT.match(title) or len(title) < 6:
            alternative = _title_from_container(container)
            if alternative:
                title = alternative

        title = _clean(title)
        title = title.strip(" -–—|·•:,")
        if len(title) > 200:
            title = title[:200].rstrip(" -–—|·•:,")

        if len(title) < 6:
            continue

        if not _is_relevant_job(title, link, base_url):
            continue

        context = ""
        if container is not None:
            try:
                context = _clean(container.get_text(" "))
            except Exception:
                context = ""
        if len(context) < len(title):
            context = title

        deadline = _find_deadline(context)
        pay_grade = _find_pay_grade(context)
        workload = _find_workload(context)
        description = _build_description(context, title)

        seen.add(key)
        results.append(
            {
                "title": title,
                "link": link,
                "institution": institution,
                "pay_grade": pay_grade,
                "workload": workload,
                "deadline": deadline,
                "description": description,
            }
        )

    return results
