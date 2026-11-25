# File: core/cover_letter_generator.py
# Version: 0.9.0 | Release Date: 2026-11-24
# EN: Dedicated module for generating German (Anschreiben) and English (Cover Letter)
#     application drafts tailored to vacancy metadata and CV skills, with optional LLM polish.
# DE: Eigenständiges Modul zur Erstellung von Anschreiben (DE) und Cover Letters (EN)
#     passend zur Stellenausschreibung und zum Lebenslauf.

from __future__ import annotations

import os
from typing import Any, Dict

import httpx


def build_letter_draft(job: Dict[str, Any], cv: Dict[str, Any], lang: str = "de") -> str:
    title = str(job.get("title") or "Wissenschaftliche Position")
    institution = str(job.get("institution") or "Hochschule / Forschungseinrichtung")
    location = str(job.get("location") or "Deutschland")
    pay_grade = str(job.get("pay_grade") or "TV-L")
    workload = str(job.get("workload") or "100%")
    kennziffer = str(job.get("kennziffer") or "")
    url = str(job.get("url") or "")
    skills = [str(s) for s in (cv.get("skills") or [])[:8]]
    skills_str = ", ".join(skills) if skills else "Python, Research Information Systems, RDM, AI Automation"
    cv_name = str(cv.get("filename") or "Bewerberprofil")
    kz_part = f" (Kennziffer: {kennziffer})" if kennziffer else ""
    ref_part = f" (Ref.: {kennziffer})" if kennziffer else ""
    url_part = f"Ausschreibungslink: {url}\n" if url else ""
    en_url_part = f"Posting reference: {url}\n" if url else ""

    if lang.lower().startswith("en"):
        return (
            f"Subject: Application for {title}{ref_part}\n\n"
            f"Dear Hiring Committee at {institution},\n\n"
            f"I am writing to express my strong interest in the position of \"{title}\" "
            f"({location}, {pay_grade}, {workload}). Based on my background profile ({cv_name}), "
            f"I bring hands-on experience in {skills_str}, combining software engineering, "
            f"research data/information architecture, and structured project coordination.\n\n"
            f"I am eager to contribute to {institution}'s research and innovation goals and look forward "
            f"to discussing how my technical and organizational expertise aligns with this role.\n"
            f"{en_url_part}\nSincerely,\n[Your Name]"
        )

    return (
        f"Bewerbung um die Stelle als „{title}“{kz_part}\n\n"
        f"Sehr geehrte Damen und Herren,\n\n"
        f"mit großem Interesse bewerbe ich mich auf die ausgeschriebene Position „{title}“ "
        f"bei {institution} ({location}, Vergütung: {pay_grade}, Umfang: {workload}). "
        f"Auf Basis meines hinterlegten Profils ({cv_name}) bringe ich fundierte Praxis in den Bereichen "
        f"{skills_str} mit und verbinde technische Systementwicklung mit Forschungsorganisation und Transfer.\n\n"
        f"Gerne unterstütze ich {institution} mit einer strukturierten, lösungsorientierten Arbeitsweise "
        f"und freue mich auf ein persönliches oder digitales Gespräch.\n"
        f"{url_part}\nMit freundlichen Grüßen\n[Ihr Name]"
    )


async def polish_letter_via_deepseek(draft: str, lang: str = "de") -> str:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return draft
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").strip().rstrip("/")
    model = os.getenv("PYCOGNITION_FAST_MODEL", "deepseek-chat").strip()
    target_lang = "English" if lang.lower().startswith("en") else "German"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=8.0)) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "temperature": 0.3,
                    "messages": [
                        {
                            "role": "system",
                            "content": f"Polish this academic job application letter in natural, idiomatic {target_lang}. Keep facts accurate and concise.",
                        },
                        {"role": "user", "content": draft},
                    ],
                },
            )
            resp.raise_for_status()
            polished = (resp.json()["choices"][0]["message"]["content"] or "").strip()
            return polished if len(polished) > 60 else draft
    except Exception:
        return draft
