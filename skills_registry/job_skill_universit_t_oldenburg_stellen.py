# File: skills_registry/job_skill_universit_t_oldenburg_stellen.py
# Version: 0.4.1 | Release Date: 2026-11-24
# Source: Universität Oldenburg Stellen
# Auto-crystallized and AST-verified by PyCognition SkillCrystallizer.

from bs4 import BeautifulSoup
from urllib.parse import urljoin
import re


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def extract(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict] = []
    seen: set[str] = set()

    # Explicitly reject non-job link patterns
    reject_patterns = (
        "/impressum",
        "/imprint",
        "/datenschutz",
        "/privacy",
        "/barrierefreiheit",
        "/sitemap",
        "/kontakt",
        "/bewerbungsportal",
        "/berufungsportal",
        "/rss-feeds",
        "/freiwilligendienste",
        "/berufsausbildung",
        "/karriereberatung",
    )

    # Each posting is rendered as div.job.row.inhouse
    for job in soup.select("div.job.row.inhouse"):
        title_el = job.select_one("h3.title a")
        if not title_el:
            continue
        title = _clean(title_el.get_text(" ", strip=True))
        if not title:
            continue

        href = (title_el.get("href") or "").strip()
        if not href:
            continue
        link = urljoin(base_url, href)
        lower_link = link.lower()

        if link in seen:
            continue
        # Must be an individual job detail page
        if "/job/" not in lower_link:
            continue
        if any(bad in lower_link for bad in reject_patterns):
            continue
        seen.add(link)

        # Institution / organisational unit
        inst_el = job.select_one(".einr .inst")
        institution = _clean(inst_el.get_text(" ", strip=True)) if inst_el else ""

        # Pay grade (from data attribute first, else parsed text)
        pay_grade = _clean(job.get("data-paygrade") or "")
        if not pay_grade:
            pg_el = job.select_one("p.paygrade")
            if pg_el:
                txt = _clean(pg_el.get_text(" ", strip=True))
                txt = re.sub(
                    r"^(?:Vergütung|Wertigkeit|Besoldung|Entgelt)\s*:?\s*",
                    "",
                    txt,
                    flags=re.IGNORECASE,
                )
                pay_grade = _clean(txt)

        # Workload / employment scope
        workload = ""
        wh_el = job.select_one("p.workinghours")
        if wh_el:
            txt = _clean(wh_el.get_text(" ", strip=True))
            txt = re.sub(
                r"^(?:Stellenumfang|Arbeitszeit|Umfang)\s*:?\s*",
                "",
                txt,
                flags=re.IGNORECASE,
            )
            workload = _clean(txt)

        # Deadline: prefer ISO machine value, output DD.MM.YYYY
        deadline = ""
        iso = (job.get("data-dateapplicationdeadline") or "").strip()
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso)
        if m:
            deadline = f"{m.group(3)}.{m.group(2)}.{m.group(1)}"
        else:
            dl_el = job.select_one("p.bewschluss")
            if dl_el:
                txt = _clean(dl_el.get_text(" ", strip=True))
                txt = re.sub(
                    r"^(?:Bewerbungsfrist|Bewerbungsschluss)\s*:?\s*",
                    "",
                    txt,
                    flags=re.IGNORECASE,
                )
                deadline = _clean(txt)

        # Description: contextual metadata from the listing block
        desc_parts: list[str] = []
        jobtype = _clean(job.get("data-jobtype") or "")
        if jobtype:
            desc_parts.append(f"Kategorie: {jobtype}")
        location = _clean(job.get("data-location") or "")
        if location:
            desc_parts.append(f"Ort: {location}")
        voc = _clean(job.get("data-vocationalfields") or "")
        if voc:
            desc_parts.append(f"Fachrichtungen: {voc}")
        # duration (Befristet) hint
        for p in job.find_all("p"):
            t = _clean(p.get_text(" ", strip=True))
            if t.lower().startswith("befristet"):
                desc_parts.append(t)
                break
        description = " | ".join(desc_parts)

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
