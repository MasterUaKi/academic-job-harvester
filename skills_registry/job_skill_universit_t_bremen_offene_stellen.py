# File: skills_registry/job_skill_universit_t_bremen_offene_stellen.py
# Version: 0.4.1 | Release Date: 2026-11-24
# Source: Universität Bremen Offene Stellen
# Auto-crystallized and AST-verified by PyCognition SkillCrystallizer.

# Crystallized System 1 Job Skill for Universität Bremen Offene Stellen
import re
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

POSTING_RE = re.compile(
    r"(/job/|/jobs/[^/]+|/stellenangebote/[^/]+|/stelle/[^/]+|/stellenangebot/|"
    r"/karriere/[^/]+|/vacanc|/position/|/offene-stellen/[^/]+|/jobboerse/|"
    r"stellenangebote/.*\.pdf$|stellen/.*\.pdf$)",
    re.IGNORECASE,
)
NOISE_RE = re.compile(
    r"(/impressum|/datenschutz|/kontakt|/ueber-uns|/studium|/lehre|/presse|"
    r"/aktuelles|/forschung/?$|/akademische-karrierewege/|/infoportal|/jobs\.html$)",
    re.IGNORECASE,
)
GRADE_RE = re.compile(r"\b(TV-?L\s*E?\s*1[345]|TVöD\s*E?\s*1[345]|E\s*1[345]|W\s*[123]|LfbA)\b", re.IGNORECASE)
WORKLOAD_RE = re.compile(r"\b(100\s*%|vollzeit|75\s*%|50\s*%|80\s*%|teilzeit)\b", re.IGNORECASE)
DEADLINE_RE = re.compile(r"(?:bewerbungsfrist|frist|bis zum|deadline)[:\s]*([0-3]?\d\.[01]?\d\.20\d{2}|20\d{2}-[01]\d-[0-3]\d)", re.IGNORECASE)


def extract(html: str, base_url: str = "https://www.uni-bremen.de/universitaet/die-uni-als-arbeitgeber/offene-stellen") -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()
    base_clean = base_url.rstrip("/").lower()

    for a in soup.find_all("a", href=True):
        raw_href = str(a["href"]).strip()
        if not raw_href or raw_href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        full_url = urljoin(base_url, raw_href).split("#")[0]
        if full_url in seen or full_url.rstrip("/").lower() == base_clean:
            continue
        parsed = urlparse(full_url)
        if parsed.scheme not in ("http", "https") or not parsed.path or parsed.path in ("/", "/de", "/de/"):
            continue
        if NOISE_RE.search(parsed.path):
            continue
        path_q = f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path
        if not POSTING_RE.search(path_q):
            continue

        txt = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        txt = re.sub(r"\s*\(PDF[^)]*\)\s*$", "", txt, flags=re.IGNORECASE).strip()
        if len(txt) < 10 or len(txt) > 220:
            continue

        parent = a.find_parent(["tr", "li", "article", "div"])
        ctx = re.sub(r"\s+", " ", parent.get_text(" ", strip=True)).strip() if parent else txt
        combined = f"{txt} {ctx}"

        gm = GRADE_RE.search(combined)
        wm = WORKLOAD_RE.search(combined)
        dm = DEADLINE_RE.search(combined)

        seen.add(full_url)
        results.append({
            "title": txt,
            "link": full_url,
            "institution": "Universität Bremen Offene Stellen",
            "pay_grade": re.sub(r"\s+", " ", gm.group(0)).upper() if gm else "TV-L / Tarif",
            "workload": re.sub(r"\s+", " ", wm.group(0)).upper() if wm else "Siehe Ausschreibung",
            "deadline": dm.group(1) if dm else "Siehe Ausschreibung",
            "description": ctx[:450],
        })
    return results
