# File: core/cognitive_job_engine.py
# Version: 0.4.1 | Release Date: 2026-11-24
# EN: PyCognition v1.3.0 System 1 / System 2 Engine for Academic Job Harvester:
#     Fixes initialization order so `self.stats` exists before `self.load_sources()` calls `self.save_sources()`.
# DE: PyCognition v1.3.0 System-1/System-2-Engine für Academic Job Harvester (Initialisierungsreihenfolge korrigiert).

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

_LOCAL_PYCOGNITION_SRC = Path(r"C:\Entwiklung\pycognition\src")
if _LOCAL_PYCOGNITION_SRC.exists() and str(_LOCAL_PYCOGNITION_SRC) not in sys.path:
    sys.path.insert(0, str(_LOCAL_PYCOGNITION_SRC))

from pycognition.skills import (
    AnomalyThresholds,
    ExecutionScope,
    SkillCapability,
    SkillCrystallizer,
    SkillExecutionTelemetry,
    TelemetryEvaluator,
)

logger = logging.getLogger("AcademicJobHarvester.CognitiveEngine")


class CognitiveJobSkillEngine:
    """
    EN: Dual-process System 1 / System 2 engine powered by PyCognition v1.3.0:
        - System 2 discovers academic job portals, synthesizes Python extractors,
          and crystallizes them into `skills_registry/` after AST & sandbox checks.
        - System 1 runs crystallized skills without LLM calls and monitors telemetry.
    DE: Dual-Process System-1/System-2-Engine auf Basis von PyCognition v1.3.0.
    """

    ALLOWED_SKILL_MODULES: Set[str] = {
        "bs4", "re", "json", "typing", "datetime", "urllib", "math", "html"
    }

    CANDIDATE_ACADEMIC_HUBS: Tuple[Dict[str, str], ...] = (
        {
            "name": "Universität Vechta Stellenangebote",
            "institution": "Universität Vechta",
            "location": "Vechta (Niedersachsen)",
            "url": "https://karriere.uni-vechta.de/stellenangebote",
        },
        {
            "name": "Universität Oldenburg Stellen",
            "institution": "Carl von Ossietzky Universität Oldenburg",
            "location": "Oldenburg (Niedersachsen)",
            "url": "https://uol.de/stellen",
        },
        {
            "name": "Leibniz Universität Hannover Stellenangebote",
            "institution": "Leibniz Universität Hannover",
            "location": "Hannover (Niedersachsen)",
            "url": "https://www.uni-hannover.de/de/universitaet/arbeiten-an-der-uni-hannover/stellenangebote",
        },
        {
            "name": "TU Braunschweig Stellenmarkt",
            "institution": "Technische Universität Braunschweig",
            "location": "Braunschweig (Niedersachsen)",
            "url": "https://www.tu-braunschweig.de/stellenmarkt",
        },
        {
            "name": "Universität Göttingen Stellenangebote",
            "institution": "Georg-August-Universität Göttingen",
            "location": "Göttingen (Niedersachsen)",
            "url": "https://www.uni-goettingen.de/de/305402.html",
        },
        {
            "name": "Universität Bremen Offene Stellen",
            "institution": "Universität Bremen",
            "location": "Bremen",
            "url": "https://www.uni-bremen.de/universitaet/die-uni-als-arbeitgeber/offene-stellen",
        },
        {
            "name": "Hochschule Bremen Stellenangebote",
            "institution": "Hochschule Bremen",
            "location": "Bremen",
            "url": "https://www.hs-bremen.de/ueber-uns/karriere/stellenangebote/",
        },
        {
            "name": "Hochschule Osnabrück Stellenangebote",
            "institution": "Hochschule Osnabrück",
            "location": "Osnabrück (Niedersachsen)",
            "url": "https://www.hs-osnabrueck.de/wir/jobs-und-karriere/stellenangebote/",
        },
        {
            "name": "Leuphana Universität Lüneburg Stellen",
            "institution": "Leuphana Universität Lüneburg",
            "location": "Lüneburg (Niedersachsen)",
            "url": "https://www.leuphana.de/universitaet/jobs-und-karriere/wissenschaft.html",
        },
        {
            "name": "TIB Hannover Stellenangebote",
            "institution": "Technische Informationsbibliothek (TIB)",
            "location": "Hannover (Niedersachsen)",
            "url": "https://www.tib.eu/de/die-tib/karriere-und-ausbildung/aktuelle-stellenangebote",
        },
    )

    DISCOVERY_DIRECTORIES: Tuple[str, ...] = (
        "https://www.mwk.niedersachsen.de/startseite/hochschulen/hochschulen_in_niedersachsen/",
        "https://www.academics.de/jobs",
    )

    CAREER_LINK_HINT_RE = re.compile(
        r"(stellenangebot|offene[-_ ]stellen|stellenmarkt|karriere|vacanc|jobs|wissenschaftliche[-_ ]mitarbeiter)",
        re.IGNORECASE,
    )

    EXCLUDED_NOISE_RE = re.compile(
        r"(/impressum|/datenschutz|/kontakt|/ueber-uns|/studium|/lehre|/presse|"
        r"/aktuelles|/forschung/?$|/akademische-karrierewege/|/infoportal)",
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

    def __init__(self, base_dir: Path, data_dir: Path) -> None:
        self.base_dir = base_dir
        self.data_dir = data_dir
        self.skills_dir = self.base_dir / "skills_registry"
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.registry_file = self.data_dir / "discovered_job_sources.json"

        self._load_dotenv_files()
        self.deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "").strip()
        self.deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").strip().rstrip("/")
        self.fast_model: str = os.getenv("PYCOGNITION_FAST_MODEL", "deepseek-chat").strip()
        self.reasoner_model: str = os.getenv("PYCOGNITION_REASONER_MODEL", "deepseek-reasoner").strip()

        self.telemetry_evaluator = TelemetryEvaluator(
            required_key="title",
            min_length=8,
            max_length=240,
            thresholds=AnomalyThresholds(
                max_corruption_ratio=0.25,
                min_sample_size_for_ratio=2,
                trigger_on_zero_yield_first_pass=True,
            ),
            custom_validator=self._validate_job_candidate,
        )
        self.crystallizer = SkillCrystallizer(
            registry_dir=self.skills_dir,
            evaluator=self.telemetry_evaluator,
            version_tag="0.4.1",
        )

        self._loaded_skills: Dict[str, Any] = {}
        self.stats: Dict[str, Any] = {
            "system1_skills_count": len(list(self.skills_dir.glob("job_skill_*.py"))),
            "system1_executions": 0,
            "system1_avg_ms": 0.0,
            "system2_synthesized": 0,
            "system2_repaired": 0,
            "system2_llm_calls": 0,
            "discovered_sources_count": 0,
            "last_system2_log": "Готов к автономному поиску источников и синтезу адаптеров",
        }
        initial_sources = self.load_sources()
        self.stats["discovered_sources_count"] = len(initial_sources)

    def _load_dotenv_files(self) -> None:
        candidates = (
            self.base_dir / ".env",
            self.base_dir.parent / "pycognition" / ".env",
            self.base_dir.parent / "grant-harvester" / ".env",
        )
        for env_file in candidates:
            if not env_file.is_file():
                continue
            for raw_line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and v and not os.getenv(k):
                    os.environ[k] = v

    def load_sources(self) -> List[Dict[str, Any]]:
        if self.registry_file.is_file():
            try:
                data = json.loads(self.registry_file.read_text(encoding="utf-8"))
                if isinstance(data, list) and data:
                    return data
            except Exception:
                pass
        initial = [
            dict(item, enabled=True, discovered_by="Seed / Niedersachsen")
            for item in self.CANDIDATE_ACADEMIC_HUBS
        ]
        self.save_sources(initial)
        return initial

    def save_sources(self, sources: List[Dict[str, Any]]) -> None:
        self.registry_file.write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")
        if hasattr(self, "stats") and isinstance(self.stats, dict):
            self.stats["discovered_sources_count"] = len(sources)

    def list_skills_status(self) -> List[Dict[str, Any]]:
        sources = self.load_sources()
        result: List[Dict[str, Any]] = []
        for src in sources:
            skill_name = self.slugify_skill_name(src.get("name") or src.get("url") or "source")
            skill_path = self.skills_dir / f"{skill_name}.py"
            result.append({
                "name": src.get("name"),
                "institution": src.get("institution"),
                "location": src.get("location"),
                "url": src.get("url"),
                "skill_name": skill_name,
                "crystallized": skill_path.is_file(),
                "discovered_by": src.get("discovered_by", "System 2"),
            })
        self.stats["system1_skills_count"] = sum(1 for r in result if r["crystallized"])
        return result

    async def discover_and_build_adapters(self, max_new_sources: int = 4) -> Dict[str, Any]:
        sources = self.load_sources()
        known_urls = {str(s.get("url") or "").rstrip("/").lower() for s in sources}
        newly_discovered = 0
        newly_synthesized = 0

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=7.0),
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
                ),
                "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8",
            },
        ) as client:
            for dir_url in self.DISCOVERY_DIRECTORIES:
                if newly_discovered >= max_new_sources:
                    break
                try:
                    resp = await client.get(dir_url, timeout=9.0)
                    if resp.status_code != 200:
                        continue
                    soup = BeautifulSoup(resp.text, "html.parser")
                    for a in soup.find_all("a", href=True):
                        href = urljoin(dir_url, str(a["href"]).strip()).split("#")[0]
                        norm_h = href.rstrip("/").lower()
                        if norm_h in known_urls or not href.startswith(("https://", "http://")):
                            continue
                        anchor_txt = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
                        if not (
                            self.CAREER_LINK_HINT_RE.search(href)
                            or self.CAREER_LINK_HINT_RE.search(anchor_txt)
                        ):
                            continue
                        host = urlparse(href).netloc.lower()
                        if not any(tld in host for tld in (".de", ".eu", ".org", ".ac.")):
                            continue
                        inst_label = anchor_txt if 4 <= len(anchor_txt) <= 90 else host
                        sources.append({
                            "name": f"{inst_label} ({host})",
                            "institution": inst_label,
                            "location": "Deutschland / EU",
                            "url": href,
                            "enabled": True,
                            "discovered_by": "System 2 Link Miner",
                        })
                        known_urls.add(norm_h)
                        newly_discovered += 1
                        if newly_discovered >= max_new_sources:
                            break
                except Exception as exc:
                    logger.debug(f"[System 2 Discovery] Directory probe skipped ({dir_url}): {exc}")

            if newly_discovered > 0:
                self.save_sources(sources)

            for src in sources:
                if not src.get("enabled", True):
                    continue
                src_name = str(src.get("name") or src.get("url"))
                src_url = str(src.get("url") or "")
                skill_name = self.slugify_skill_name(src_name)
                if (self.skills_dir / f"{skill_name}.py").is_file():
                    continue

                ok = await self._synthesize_skill_for_url(
                    client=client,
                    skill_name=skill_name,
                    source_name=src_name,
                    url=src_url,
                )
                if ok:
                    newly_synthesized += 1

        self.list_skills_status()
        self.stats["last_system2_log"] = (
            f"Система 2: найдено источников +{newly_discovered}, "
            f"синтезировано AST-адаптеров +{newly_synthesized} (всего скиллов: {self.stats['system1_skills_count']})"
        )
        return {
            "newly_discovered": newly_discovered,
            "newly_synthesized": newly_synthesized,
            "total_skills": self.stats["system1_skills_count"],
            "llm_mode": "DeepSeek System 2" if self.deepseek_api_key else "Deterministic Structural Synthesizer",
        }

    async def teach_new_source(self, url: str, name: str = "", location: str = "Deutschland / EU") -> Dict[str, Any]:
        clean_url = url.strip()
        if not clean_url.startswith(("http://", "https://")):
            clean_url = f"https://{clean_url}"

        host = urlparse(clean_url).netloc or "custom-portal"
        clean_name = name.strip() or f"Portal {host}"
        skill_name = self.slugify_skill_name(clean_name)

        sources = self.load_sources()
        if not any(str(s.get("url")).rstrip("/").lower() == clean_url.rstrip("/").lower() for s in sources):
            sources.insert(0, {
                "name": clean_name,
                "institution": clean_name,
                "location": location,
                "url": clean_url,
                "enabled": True,
                "discovered_by": "User Taught -> System 2",
            })
            self.save_sources(sources)

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(16.0, connect=7.0),
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        ) as client:
            synthesized = await self._synthesize_skill_for_url(
                client=client,
                skill_name=skill_name,
                source_name=clean_name,
                url=clean_url,
                force_rebuild=True,
            )

        self.list_skills_status()
        self.stats["last_system2_log"] = (
            f"Система 2 обучена для «{clean_name}» -> скилл {skill_name}.py "
            f"({'успешно кристаллизован' if synthesized else 'ошибка синтеза'})"
        )
        return {
            "status": "ok" if synthesized else "failed",
            "skill_name": skill_name,
            "source_name": clean_name,
            "url": clean_url,
        }

    async def _synthesize_skill_for_url(
        self,
        client: httpx.AsyncClient,
        skill_name: str,
        source_name: str,
        url: str,
        current_code: Optional[str] = None,
        diagnostic_report: str = "",
        baseline_valid_count: int = 0,
        force_rebuild: bool = False,
    ) -> bool:
        try:
            resp = await client.get(url, timeout=10.0)
            if resp.status_code != 200 or len(resp.text) < 400:
                return False
            html = resp.text
        except Exception as exc:
            logger.warning(f"[System 2] Cannot fetch {url}: {exc}")
            return False

        candidate_code = ""
        if self.deepseek_api_key:
            candidate_code = await self._synthesize_via_deepseek(
                skill_name=skill_name,
                source_name=source_name,
                url=url,
                html=html,
                current_code=current_code,
                diagnostic_report=diagnostic_report,
            )

        if not candidate_code:
            candidate_code = self._build_deterministic_extractor_code(source_name=source_name, seed_url=url)

        if force_rebuild:
            existing_file = self.skills_dir / f"{skill_name}.py"
            if existing_file.exists():
                existing_file.unlink()
            self._loaded_skills.pop(skill_name, None)

        capability = SkillCapability(
            skill_name=skill_name,
            allowed_scope=ExecutionScope.PARSE_DATA,
            allowed_modules=set(self.ALLOWED_SKILL_MODULES),
            required_entrypoint="extract",
        )
        cryst_result = self.crystallizer.crystallize(
            source_code=candidate_code,
            capability=capability,
            sample_args=(html, url),
            target_uri=url,
            baseline_valid_count=baseline_valid_count,
            max_allowed_corruption=0.25,
            source_label=source_name,
        )

        if not cryst_result.accepted and self.deepseek_api_key:
            fallback_code = self._build_deterministic_extractor_code(source_name=source_name, seed_url=url)
            cryst_result = self.crystallizer.crystallize(
                source_code=fallback_code,
                capability=capability,
                sample_args=(html, url),
                target_uri=url,
                baseline_valid_count=baseline_valid_count,
                max_allowed_corruption=0.25,
                source_label=source_name,
            )

        if cryst_result.accepted:
            self.stats["system2_synthesized"] += 1
            self._loaded_skills.pop(skill_name, None)
            return True

        logger.warning(f"[SkillCrystallizer] Rejected '{skill_name}': {cryst_result.reason}")
        return False

    async def _synthesize_via_deepseek(
        self,
        skill_name: str,
        source_name: str,
        url: str,
        html: str,
        current_code: Optional[str],
        diagnostic_report: str,
    ) -> str:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "svg", "noscript", "iframe", "meta", "link"]):
            tag.decompose()
        main_node = soup.find("main") or soup.find("body") or soup
        distilled_dom = re.sub(r"\n\s*\n+", "\n", str(main_node))[:20000]

        existing_block = ""
        if current_code:
            existing_block = "Current code:\n```python\n" + current_code + "\n```\n"

        self.stats["system2_llm_calls"] += 1
        raw_reply = await self._call_llm(
            model=self.reasoner_model,
            system_prompt=(
                "You are the System 2 Cognitive Architect of PyCognition. Write a deterministic Python "
                "extractor for academic/university job postings using BeautifulSoup and urllib.parse.urljoin.\n"
                "STRICT AST SECURITY RULES:\n"
                "1. Define `def extract(html: str, base_url: str) -> list[dict]:`.\n"
                "2. Allowed imports ONLY: `bs4`, `re`, `json`, `typing`, `datetime`, `urllib.parse`, `html`.\n"
                "3. NEVER import `os`, `sys`, `subprocess`, `requests`, `httpx`, or `pathlib`.\n"
                "4. Extract ONLY individual job postings (PDFs or job detail pages). Reject navigation links, "
                "imprint, privacy, or general research overview pages.\n"
                "5. Each dict must include: `title`, `link`, `institution`, `pay_grade`, `workload`, `deadline`, `description`.\n"
                "6. Output ONLY valid Python code inside ```python ... ```."
            ),
            user_prompt=(
                f"Source: {source_name}\nURL: {url}\n"
                f"Diagnostics: {diagnostic_report or 'Initial synthesis'}\n"
                f"{existing_block}"
                f"DOM:\n```html\n{distilled_dom}\n```"
            ),
        )
        m = re.search(r"```python\s*(.*?)```", raw_reply, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _build_deterministic_extractor_code(source_name: str, seed_url: str) -> str:
        safe_source = source_name.replace('"', "'")
        safe_seed = seed_url.replace('"', "'")
        return f"""# Crystallized System 1 Job Skill for {safe_source}
import re
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup

POSTING_RE = re.compile(
    r"(/job/|/jobs/[^/]+|/stellenangebote/[^/]+|/stelle/[^/]+|/stellenangebot/|"
    r"/karriere/[^/]+|/vacanc|/position/|/offene-stellen/[^/]+|/jobboerse/|"
    r"stellenangebote/.*\\.pdf$|stellen/.*\\.pdf$)",
    re.IGNORECASE,
)
NOISE_RE = re.compile(
    r"(/impressum|/datenschutz|/kontakt|/ueber-uns|/studium|/lehre|/presse|"
    r"/aktuelles|/forschung/?$|/akademische-karrierewege/|/infoportal|/jobs\\.html$)",
    re.IGNORECASE,
)
GRADE_RE = re.compile(r"\\b(TV-?L\\s*E?\\s*1[345]|TVöD\\s*E?\\s*1[345]|E\\s*1[345]|W\\s*[123]|LfbA)\\b", re.IGNORECASE)
WORKLOAD_RE = re.compile(r"\\b(100\\s*%|vollzeit|75\\s*%|50\\s*%|80\\s*%|teilzeit)\\b", re.IGNORECASE)
DEADLINE_RE = re.compile(r"(?:bewerbungsfrist|frist|bis zum|deadline)[:\\s]*([0-3]?\\d\\.[01]?\\d\\.20\\d{{2}}|20\\d{{2}}-[01]\\d-[0-3]\\d)", re.IGNORECASE)


def extract(html: str, base_url: str = "{safe_seed}") -> list[dict]:
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
        path_q = f"{{parsed.path}}?{{parsed.query}}" if parsed.query else parsed.path
        if not POSTING_RE.search(path_q):
            continue

        txt = re.sub(r"\\s+", " ", a.get_text(" ", strip=True)).strip()
        txt = re.sub(r"\\s*\\(PDF[^)]*\\)\\s*$", "", txt, flags=re.IGNORECASE).strip()
        if len(txt) < 10 or len(txt) > 220:
            continue

        parent = a.find_parent(["tr", "li", "article", "div"])
        ctx = re.sub(r"\\s+", " ", parent.get_text(" ", strip=True)).strip() if parent else txt
        combined = f"{{txt}} {{ctx}}"

        gm = GRADE_RE.search(combined)
        wm = WORKLOAD_RE.search(combined)
        dm = DEADLINE_RE.search(combined)

        seen.add(full_url)
        results.append({{
            "title": txt,
            "link": full_url,
            "institution": "{safe_source}",
            "pay_grade": re.sub(r"\\s+", " ", gm.group(0)).upper() if gm else "TV-L / Tarif",
            "workload": re.sub(r"\\s+", " ", wm.group(0)).upper() if wm else "Siehe Ausschreibung",
            "deadline": dm.group(1) if dm else "Siehe Ausschreibung",
            "description": ctx[:450],
        }})
    return results
"""

    async def _call_llm(self, model: str, system_prompt: str, user_prompt: str) -> str:
        if not self.deepseek_api_key:
            return ""
        endpoint = f"{self.deepseek_base_url}/chat/completions"
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if "reasoner" not in model.lower():
            payload["temperature"] = 0.1
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=12.0)) as client:
                resp = await client.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {self.deepseek_api_key}", "Content-Type": "application/json"},
                    json=payload,
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"] or ""
        except Exception as exc:
            logger.error(f"[System 2 LLM Error] {exc}")
            return ""

    async def run_system1_harvest(self) -> List[Dict[str, Any]]:
        sources = self.load_sources()
        all_records: List[Dict[str, Any]] = []
        seen_urls: Set[str] = set()
        total_exec_ms = 0.0
        exec_count = 0

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=6.0),
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
                ),
                "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8",
            },
        ) as client:
            for src in sources:
                if not src.get("enabled", True):
                    continue
                src_name = str(src.get("name") or src.get("url"))
                inst_name = str(src.get("institution") or src_name)
                loc_str = str(src.get("location") or "Deutschland")
                src_url = str(src.get("url") or "")
                skill_name = self.slugify_skill_name(src_name)

                skill_mod = self._load_skill_module(skill_name)
                if skill_mod is None:
                    synthesized = await self._synthesize_skill_for_url(
                        client=client,
                        skill_name=skill_name,
                        source_name=src_name,
                        url=src_url,
                    )
                    if synthesized:
                        skill_mod = self._load_skill_module(skill_name, force_reload=True)
                if skill_mod is None:
                    continue

                try:
                    resp = await client.get(src_url, timeout=9.0)
                    if resp.status_code != 200 or len(resp.text) < 300:
                        continue
                    html = resp.text
                except Exception:
                    continue

                t0 = time.perf_counter()
                telemetry = self._execute_skill(skill_mod, skill_name, src_url, html)
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                total_exec_ms += elapsed_ms
                exec_count += 1

                if telemetry.is_degraded:
                    logger.warning(f"[System 2 Self-Healing] Repairing degraded skill '{skill_name}'...")
                    current_code = (self.skills_dir / f"{skill_name}.py").read_text(encoding="utf-8", errors="ignore")
                    repaired = await self._synthesize_skill_for_url(
                        client=client,
                        skill_name=skill_name,
                        source_name=src_name,
                        url=src_url,
                        current_code=current_code,
                        diagnostic_report=telemetry.to_diagnostic_prompt(),
                        baseline_valid_count=telemetry.valid_count,
                        force_rebuild=True,
                    )
                    if repaired:
                        self.stats["system2_repaired"] += 1
                        skill_mod = self._load_skill_module(skill_name, force_reload=True)
                        if skill_mod is not None:
                            telemetry = self._execute_skill(skill_mod, skill_name, src_url, html)

                for item in telemetry.valid_records:
                    norm = self._normalize_skill_record(item, inst_name, loc_str, src_name)
                    if norm and norm["url"] not in seen_urls:
                        seen_urls.add(norm["url"])
                        all_records.append(norm)

        self.stats["system1_executions"] += exec_count
        if exec_count > 0:
            self.stats["system1_avg_ms"] = round(total_exec_ms / exec_count, 2)
        self.list_skills_status()
        return all_records

    def _execute_skill(
        self,
        skill_module: Any,
        skill_name: str,
        url: str,
        html: str,
    ) -> SkillExecutionTelemetry:
        extract_fn = getattr(skill_module, "extract", None)
        if not callable(extract_fn):
            return self.telemetry_evaluator.evaluate(
                skill_name=skill_name,
                target_uri=url,
                raw_output=None,
                execution_error="Missing callable extract(html, base_url).",
            )
        try:
            sig = inspect.signature(extract_fn)
            raw_out = extract_fn(html, url) if len(sig.parameters) >= 2 else extract_fn(html)
            return self.telemetry_evaluator.evaluate(
                skill_name=skill_name,
                target_uri=url,
                raw_output=raw_out,
                execution_error=None,
            )
        except Exception as exc:
            return self.telemetry_evaluator.evaluate(
                skill_name=skill_name,
                target_uri=url,
                raw_output=None,
                execution_error=f"{type(exc).__name__}: {exc}",
            )

    @classmethod
    def _validate_job_candidate(cls, record: Dict[str, Any]) -> bool:
        title = re.sub(r"\s+", " ", str(record.get("title") or "")).strip()
        link = str(record.get("link") or record.get("url") or "").strip()
        if len(title) < 8 or len(title) > 240:
            return False
        if not link.startswith(("https://", "http://")):
            return False
        parsed = urlparse(link)
        if cls.EXCLUDED_NOISE_RE.search(parsed.path):
            return False
        return True

    def _normalize_skill_record(
        self,
        raw: Dict[str, Any],
        institution: str,
        location: str,
        source_portal: str,
    ) -> Optional[Dict[str, Any]]:
        title = re.sub(r"\s+", " ", str(raw.get("title") or "")).strip()
        url = str(raw.get("link") or raw.get("url") or "").strip()
        if len(title) < 8 or not url.startswith(("https://", "http://")):
            return None

        desc = re.sub(r"\s+", " ", str(raw.get("description") or title)).strip()
        combined = f"{title} {desc} {institution}"
        gm = self.PAY_GRADE_RE.search(combined)
        wm = self.WORKLOAD_RE.search(combined)

        pay_grade = str(raw.get("pay_grade") or "").strip() or (
            re.sub(r"\s+", " ", gm.group(0)).upper() if gm else "TV-L / Tarif"
        )
        workload = str(raw.get("workload") or "").strip() or (
            re.sub(r"\s+", " ", wm.group(0)).upper() if wm else "Siehe Ausschreibung"
        )
        deadline = str(raw.get("deadline") or "Siehe Ausschreibung").strip()
        native_id = hashlib.sha256(url.lower().encode("utf-8")).hexdigest()[:16]

        tags = [
            tok.lower()
            for tok in re.findall(r"[A-Za-zÄÖÜäöüß0-9\-\+]{3,}", f"{title} {institution} {pay_grade}")
            if len(tok) >= 3
        ][:12]

        return {
            "id": f"s1-{native_id}",
            "native_id": native_id,
            "kennziffer": native_id[:10].upper(),
            "title": title[:220],
            "institution": institution[:160],
            "funder": institution[:160],
            "location": location[:120],
            "pay_grade": pay_grade[:40],
            "workload": workload[:40],
            "contract": "PDF / Ausschreibung" if url.lower().endswith(".pdf") else "System 1 Skill",
            "horizon": "H2",
            "deadline": deadline[:40],
            "tags": tags,
            "link": url,
            "url": url,
            "description": desc[:500],
            "source_portal": f"System 1 · {source_portal}",
            "source_type": "funding_opportunity",
        }

    @staticmethod
    def slugify_skill_name(source_name: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", source_name.lower()).strip("_")
        return f"job_skill_{slug[:50]}"

    def _load_skill_module(self, skill_name: str, force_reload: bool = False) -> Optional[Any]:
        if not force_reload and skill_name in self._loaded_skills:
            return self._loaded_skills[skill_name]
        skill_path = self.skills_dir / f"{skill_name}.py"
        if not skill_path.is_file():
            return None
        try:
            spec = importlib.util.spec_from_file_location(skill_name, str(skill_path))
            if spec is None or spec.loader is None:
                return None
            mod = importlib.util.module_from_spec(spec)
            sys.modules[skill_name] = mod
            spec.loader.exec_module(mod)
            self._loaded_skills[skill_name] = mod
            return mod
        except Exception as exc:
            logger.error(f"Failed to load skill '{skill_name}': {exc}")
            return None
