# File: skills_registry/job_skill_universit_t_vechta_stellenangebote.py
# Version: 0.4.1 | Release Date: 2026-11-24
# Source: Universität Vechta Stellenangebote
# Auto-crystallized and AST-verified by PyCognition SkillCrystallizer.

from bs4 import BeautifulSoup
import re
from urllib.parse import urljoin


def extract(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()

    def is_job_link(href: str) -> bool:
        if not href:
            return False
        h = href.strip().lower()
        if h.startswith(("#", "mailto:", "javascript:", "tel:")):
            return False
        if re.search(r"\.pdf(?:$|[?#])", h):
            return True
        if any(part in h for part in ("/stellenangebote/", "/stellenausschreibung", "/job/", "/jobs/")):
            return True
        return False

    def clean(text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip()

    for section in soup.select("section.cojobs"):
        heading = section.find("h2")
        category = clean(heading.get_text(" ", strip=True)) if heading else ""

        table = section.find("table")
        if not table:
            continue

        for row in table.find_all("tr"):
            if row.find("th"):
                continue

            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            title_cell = cells[1]
            row_text = clean(row.get_text(" ", strip=True))

            job_anchor = None
            for anchor in row.find_all("a", href=True):
                href = anchor.get("href", "").strip()
                if is_job_link(href):
                    job_anchor = anchor
                    break

            if job_anchor is None:
                continue

            href = job_anchor.get("href", "").strip()
            link = urljoin(base_url, href)

            strong = title_cell.find("strong")
            title = clean(strong.get_text(" ", strip=True)) if strong else clean(job_anchor.get_text(" ", strip=True))
            if not title:
                continue

            deadline = None
            mobile = title_cell.find(class_="cojobs-mobile")
            if mobile:
                match = re.search(r"Bewerbungsfrist\s*:?\s*(\d{1,2}\.\d{1,2}\.\d{4})", clean(mobile.get_text(" ", strip=True)))
                if match:
                    deadline = match.group(1)

            if not deadline:
                for cell in reversed(cells):
                    match = re.search(r"\b(\d{1,2}\.\d{1,2}\.\d{4})\b", clean(cell.get_text(" ", strip=True)))
                    if match:
                        deadline = match.group(1)
                        break

            if not deadline:
                match = re.search(r"Bewerbungsfrist\s*:?\s*(\d{1,2}\.\d{1,2}\.\d{4})", row_text)
                if match:
                    deadline = match.group(1)

            raw_description = clean(title_cell.get_text(" ", strip=True))
            if title:
                raw_description = raw_description.replace(title, " ")
            raw_description = re.sub(r"Bewerbungsfrist\s*:?\s*\d{1,2}\.\d{1,2}\.\d{4}", " ", raw_description)
            raw_description = clean(raw_description).strip(" -–|,;")

            description_parts = []
            if category:
                description_parts.append(category)
            if raw_description:
                description_parts.append(raw_description)
            description = " – ".join(description_parts) if description_parts else title

            search_text = f"{title} {description}"

            pay_grade = None
            pay_match = re.search(
                r"(?:TV-?L|TVöD|Entgeltgruppe|Besoldungsgruppe)\s*[A-Za-z0-9.\-– ]{1,25}",
                search_text,
                re.IGNORECASE,
            )
            if pay_match:
                pay_grade = clean(pay_match.group(0)).strip(" ,;.")

            workload = None
            workload_match = re.search(
                r"(?:Teilzeit|Vollzeit|\d{1,3}\s*%\s*(?:der\s*)?(?:regelmäßigen\s*)?(?:wöchentlichen\s*)?Arbeitszeit|\d{1,3}(?:[.,]\d+)?\s*(?:Stunden|Std\.?)\s*(?:/|pro)?\s*Woche)",
                search_text,
                re.IGNORECASE,
            )
            if workload_match:
                workload = clean(workload_match.group(0))

            key = (title, link)
            if key in seen:
                continue
            seen.add(key)

            results.append({
                "title": title,
                "link": link,
                "institution": "Universität Vechta",
                "pay_grade": pay_grade,
                "workload": workload,
                "deadline": deadline,
                "description": description,
            })

    return results
