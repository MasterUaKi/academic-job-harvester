# File: core/cognitive_job_engine.py
# Version: 0.9.0 | Release Date: 2026-11-24
# EN: Compact PyCognition v1.3.0 System 1 / System 2 engine orchestrating
#     System2EscalationPolicy and ProgressiveSkillSynthesizer across discovered
#     university and research portals with optional JS/Playwright rendering.
# DE: Kompakter System-1/System-2-Kern v0.9.0 mit System2EscalationPolicy und
#     ProgressiveSkillSynthesizer sowie optionalem JS/Playwright-Rendering.

from __future__ import annotations

import asyncio
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

try:
    from playwright.async_api import async_playwright
    _HAS_PLAYWRIGHT = True
except ImportError:
    async_playwright = None  # type: ignore
    _HAS_PLAYWRIGHT = False

_LOCAL_PYCOGNITION_SRC = Path(r"C:\Entwiklung\pycognition\src")
if _LOCAL_PYCOGNITION_SRC.exists() and str(_LOCAL_PYCOGNITION_SRC) not in sys.path:
    sys.path.insert(0, str(_LOCAL_PYCOGNITION_SRC))

from core.escalation_config import System2EscalationPolicy
from core.skill_synthesizer import ProgressiveSkillSynthesizer
from pycognition.skills import (
    AnomalyThresholds,
    SkillCrystallizer,
    SkillExecutionTelemetry,
    TelemetryEvaluator,
)

logger = logging.getLogger("AcademicJobHarvester.CognitiveEngine")


class CognitiveJobSkillEngine:
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
    )

    DISCOVERY_DIRECTORIES: Tuple[str, ...] = (
        "https://www.hrk.de/mitglieder/hochschulen-im-profil/",
        "https://www.hochschulkompass.de/hochschulen/hochschulen-in-deutschland.html",
        "https://www.mwk.niedersachsen.de/startseite/hochschulen/hochschulen_in_niedersachsen/",
        "https://www.academics.de/jobs",
    )

    UNIVERSITY_LINK_RE = re.compile(
        r"(universitaet|universität|hochschule|tu-|rwth|lmu|kit|charite|charité|uni-|fh-|haw-)",
        re.IGNORECASE,
    )
    CAREER_LINK_HINT_RE = re.compile(
        r"(stellenangebot|offene[-_ ]stellen|stellenmarkt|karriere|vacanc|jobs|wissenschaftliche[-_ ]mitarbeiter)",
        re.IGNORECASE,
    )
    EXCLUDED_NOISE_RE = re.compile(
        r"(/impressum|/datenschutz|/kontakt|/ueber-uns|/studium|/lehre|/presse|/aktuelles|/forschung/?$|/akademische-karrierewege/|/infoportal)",
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

    def __init__(self, base_dir: Path, data_dir: Path, enable_js_rendering: bool = True) -> None:
        self.base_dir = base_dir
        self.data_dir = data_dir
        self.enable_js_rendering = enable_js_rendering
        self.skills_dir = self.base_dir / "skills_registry"
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.registry_file = self.data_dir / "discovered_job_sources.json"
        self.escalation_cfg_path = self.base_dir / "config" / "system2_escalation.json"

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
            version_tag="0.9.0",
        )
        self.escalation_policy = System2EscalationPolicy(
            config_path=self.escalation_cfg_path,
            default_fast_model=self.fast_model,
            default_reasoner_model=self.reasoner_model,
        )
        self.synthesizer = ProgressiveSkillSynthesizer(
            crystallizer=self.crystallizer,
            policy=self.escalation_policy,
            allowed_modules=self.ALLOWED_SKILL_MODULES,
            llm_caller=self._call_llm,
            on_llm_call=lambda: self.stats.__setitem__("system2_llm_calls", self.stats["system2_llm_calls"] + 1),
            on_synthesized=lambda: self.stats.__setitem__("system2_synthesized", self.stats["system2_synthesized"] + 1),
        )

        self._loaded_skills: Dict[str, Any] = {}
        self.stats: Dict[str, Any] = {
            "system1_skills_count": len(list(self.skills_dir.glob("job_skill_*.py"))),
            "system1_executions": 0,
            "system1_avg_ms": 0.0,
            "system2_synthesized": 0,
            "system2_repaired": 0,
            "system2_llm_calls": 0,
            "system2_max_attempts": len(self.escalation_policy.steps),
            "js_rendered_pages": 0,
            "playwright_available": _HAS_PLAYWRIGHT,
            "discovered_sources_count": 0,
            "last_system2_log": f"Готов · Шагов эскалации S2: {len(self.escalation_policy.steps)}",
        }
        self.stats["discovered_sources_count"] = len(self.load_sources())

    def _load_dotenv_files(self) -> None:
        for env_file in (self.base_dir / ".env", self.base_dir.parent / "pycognition" / ".env"):
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
        initial = [dict(item, enabled=True, discovered_by="Seed / Niedersachsen") for item in self.CANDIDATE_ACADEMIC_HUBS]
        self.save_sources(initial)
        return initial

    def save_sources(self, sources: List[Dict[str, Any]]) -> None:
        self.registry_file.write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")
        if hasattr(self, "stats") and isinstance(self.stats, dict):
            self.stats["discovered_sources_count"] = len(sources)

    def list_skills_status(self) -> List[Dict[str, Any]]:
        sources = self.load_sources()
        out: List[Dict[str, Any]] = []
        for src in sources:
            sname = self.slugify_skill_name(src.get("name") or src.get("url") or "source")
            out.append({
                "name": src.get("name"),
                "institution": src.get("institution"),
                "location": src.get("location"),
                "url": src.get("url"),
                "skill_name": sname,
                "crystallized": (self.skills_dir / f"{sname}.py").is_file(),
                "discovered_by": src.get("discovered_by", "System 2"),
            })
        self.stats["system1_skills_count"] = sum(1 for r in out if r["crystallized"])
        return out

    async def _fetch_page_html(self, client: httpx.AsyncClient, url: str, timeout: float = 9.0, prefer_js: bool = False) -> str:
        http_html = ""
        try:
            resp = await client.get(url, timeout=timeout)
            if resp.status_code == 200 and len(resp.text) > 250:
                http_html = resp.text
        except Exception:
            http_html = ""
        if self.enable_js_rendering and _HAS_PLAYWRIGHT and (prefer_js or not http_html):
            rendered = await self._render_with_playwright(url)
            if rendered and len(rendered) > len(http_html):
                self.stats["js_rendered_pages"] += 1
                return rendered
        return http_html

    async def _render_with_playwright(self, url: str, timeout_ms: int = 12000) -> str:
        if not _HAS_PLAYWRIGHT or async_playwright is None:
            return ""
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    page = await browser.new_page(locale="de-DE")
                    await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                    await asyncio.sleep(0.5)
                    return await page.content()
                finally:
                    await browser.close()
        except Exception:
            return ""

    async def discover_and_build_adapters(self, max_new_sources: int = 6) -> Dict[str, Any]:
        sources = self.load_sources()
        known = {str(s.get("url") or "").rstrip("/").lower() for s in sources}
        newly_disc = newly_syn = 0
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(14.0, connect=6.0),
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            for dir_url in self.DISCOVERY_DIRECTORIES:
                if newly_disc >= max_new_sources:
                    break
                try:
                    html = await self._fetch_page_html(client, dir_url, timeout=9.0)
                    if not html:
                        continue
                    dir_host = urlparse(dir_url).netloc.lower()
                    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
                        if newly_disc >= max_new_sources:
                            break
                        href = urljoin(dir_url, str(a["href"]).strip()).split("#")[0]
                        if not href.startswith(("https://", "http://")) or href.rstrip("/").lower() in known:
                            continue
                        host = urlparse(href).netloc.lower()
                        if not host or host == dir_host or not any(t in host for t in (".de", ".eu", ".org", ".ac.")):
                            continue
                        txt = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
                        if not (
                            self.CAREER_LINK_HINT_RE.search(href)
                            or self.CAREER_LINK_HINT_RE.search(txt)
                            or self.UNIVERSITY_LINK_RE.search(f"{host} {txt}")
                        ):
                            continue
                        label = txt if 4 <= len(txt) <= 85 else host
                        sources.append({
                            "name": f"{label} ({host})",
                            "institution": label,
                            "location": "Deutschland (HRK)",
                            "url": href,
                            "enabled": True,
                            "discovered_by": f"System 2 ({dir_host})",
                        })
                        known.add(href.rstrip("/").lower())
                        newly_disc += 1
                except Exception as exc:
                    logger.debug(f"[Discovery] {dir_url}: {exc}")
            if newly_disc:
                self.save_sources(sources)
            for src in sources:
                if not src.get("enabled", True):
                    continue
                sname = self.slugify_skill_name(src.get("name") or src.get("url") or "src")
                if (self.skills_dir / f"{sname}.py").is_file():
                    continue
                res = await self._synthesize_skill_for_url(
                    client, sname, str(src.get("name") or src.get("url")), str(src.get("url") or "")
                )
                if res.get("accepted"):
                    newly_syn += 1
        self.list_skills_status()
        self.stats["last_system2_log"] = (
            f"Система 2 ({len(self.escalation_policy.steps)} шагов): +{newly_disc} источников, +{newly_syn} скиллов"
        )
        return {"newly_discovered": newly_disc, "newly_synthesized": newly_syn, "total_skills": self.stats["system1_skills_count"]}

    async def teach_new_source(self, url: str, name: str = "", location: str = "Deutschland / EU") -> Dict[str, Any]:
        clean_url = url.strip()
        if not clean_url.startswith(("http://", "https://")):
            clean_url = f"https://{clean_url}"
        host = urlparse(clean_url).netloc or "custom-portal"
        clean_name = name.strip() or f"Portal {host}"
        sname = self.slugify_skill_name(clean_name)
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
            timeout=httpx.Timeout(18.0, connect=7.0),
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            res = await self._synthesize_skill_for_url(client, sname, clean_name, clean_url, force_rebuild=True)
        self.list_skills_status()
        ok = bool(res.get("accepted"))
        att = res.get("attempt")
        tot = res.get("total_attempts")
        mdl = res.get("model")
        rsn = res.get("reason", "ошибка")
        self.stats["last_system2_log"] = f"Система 2 («{clean_name}», шаг {att}/{tot}, {mdl}): {rsn}"
        return {
            "status": "ok" if ok else "failed",
            "skill_name": sname,
            "source_name": clean_name,
            "url": clean_url,
            "escalation": res,
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
    ) -> Dict[str, Any]:
        html = await self._fetch_page_html(client, url, timeout=10.0)
        if not html or len(html) < 400:
            return {"accepted": False, "reason": "empty HTML"}
        if force_rebuild:
            p = self.skills_dir / f"{skill_name}.py"
            if p.exists():
                p.unlink()
            self._loaded_skills.pop(skill_name, None)
        res = await self.synthesizer.synthesize_with_escalation(
            skill_name, source_name, url, html, self.deepseek_api_key, current_code, diagnostic_report, baseline_valid_count
        )
        if res.get("accepted"):
            self._loaded_skills.pop(skill_name, None)
        return res

    async def _call_llm(self, model: str, system_prompt: str, user_prompt: str, temperature: float = 0.1) -> str:
        if not self.deepseek_api_key:
            return ""
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if "reasoner" not in model.lower():
            payload["temperature"] = temperature
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=12.0)) as client:
                resp = await client.post(
                    f"{self.deepseek_base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.deepseek_api_key}", "Content-Type": "application/json"},
                    json=payload,
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"] or ""
        except Exception as exc:
            logger.error(f"[LLM] {exc}")
            return ""

    async def run_system1_harvest(self) -> List[Dict[str, Any]]:
        sources = self.load_sources()
        records: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        total_ms = 0.0
        runs = 0
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=6.0),
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as client:
            for src in sources:
                if not src.get("enabled", True):
                    continue
                sname = self.slugify_skill_name(src.get("name") or src.get("url") or "src")
                surl = str(src.get("url") or "")
                mod = self._load_skill_module(sname)
                if mod is None:
                    syn = await self._synthesize_skill_for_url(client, sname, str(src.get("name") or surl), surl)
                    if syn.get("accepted"):
                        mod = self._load_skill_module(sname, force_reload=True)
                if mod is None:
                    continue
                html = await self._fetch_page_html(client, surl, timeout=9.0)
                if not html or len(html) < 300:
                    continue
                t0 = time.perf_counter()
                tel = self._execute_skill(mod, sname, surl, html)
                total_ms += (time.perf_counter() - t0) * 1000.0
                runs += 1
                for item in tel.valid_records:
                    norm = self._normalize_skill_record(
                        item,
                        str(src.get("institution") or sname),
                        str(src.get("location") or "Deutschland"),
                        str(src.get("name") or surl),
                    )
                    if norm and norm["url"] not in seen:
                        seen.add(norm["url"])
                        records.append(norm)
        self.stats["system1_executions"] += runs
        if runs:
            self.stats["system1_avg_ms"] = round(total_ms / runs, 2)
        self.list_skills_status()
        return records

    def _execute_skill(self, skill_module: Any, skill_name: str, url: str, html: str) -> SkillExecutionTelemetry:
        fn = getattr(skill_module, "extract", None)
        if not callable(fn):
            return self.telemetry_evaluator.evaluate(skill_name, url, None, "Missing extract")
        try:
            sig = inspect.signature(fn)
            raw = fn(html, url) if len(sig.parameters) >= 2 else fn(html)
            return self.telemetry_evaluator.evaluate(skill_name, url, raw, None)
        except Exception as exc:
            return self.telemetry_evaluator.evaluate(skill_name, url, None, f"{type(exc).__name__}: {exc}")

    @classmethod
    def _validate_job_candidate(cls, record: Dict[str, Any]) -> bool:
        title = re.sub(r"\s+", " ", str(record.get("title") or "")).strip()
        link = str(record.get("link") or record.get("url") or "").strip()
        if len(title) < 8 or len(title) > 240 or not link.startswith(("https://", "http://")):
            return False
        return not bool(cls.EXCLUDED_NOISE_RE.search(urlparse(link).path))

    def _normalize_skill_record(self, raw: Dict[str, Any], institution: str, location: str, source_portal: str) -> Optional[Dict[str, Any]]:
        title = re.sub(r"\s+", " ", str(raw.get("title") or "")).strip()
        url = str(raw.get("link") or raw.get("url") or "").strip()
        if len(title) < 8 or not url.startswith(("https://", "http://")):
            return None
        desc = re.sub(r"\s+", " ", str(raw.get("description") or title)).strip()
        comb = f"{title} {desc} {institution}"
        gm, wm = self.PAY_GRADE_RE.search(comb), self.WORKLOAD_RE.search(comb)
        pay_grade = str(raw.get("pay_grade") or "").strip() or (re.sub(r"\s+", " ", gm.group(0)).upper() if gm else "TV-L / Tarif")
        workload = str(raw.get("workload") or "").strip() or (re.sub(r"\s+", " ", wm.group(0)).upper() if wm else "Siehe Ausschreibung")
        nid = hashlib.sha256(url.lower().encode("utf-8")).hexdigest()[:16]
        tags = [t.lower() for t in re.findall(r"[A-Za-zÄÖÜäöüß0-9\-\+]{3,}", f"{title} {institution} {pay_grade}")][:12]
        return {
            "id": f"s1-{nid}",
            "native_id": nid,
            "kennziffer": nid[:10].upper(),
            "title": title[:220],
            "institution": institution[:160],
            "funder": institution[:160],
            "location": location[:120],
            "pay_grade": pay_grade[:40],
            "workload": workload[:40],
            "contract": "PDF / Ausschreibung" if url.lower().endswith(".pdf") else "System 1 Skill",
            "horizon": "H2",
            "deadline": str(raw.get("deadline") or "Siehe Ausschreibung")[:40],
            "tags": tags,
            "link": url,
            "url": url,
            "description": desc[:500],
            "source_portal": f"System 1 · {source_portal}",
            "source_type": "funding_opportunity",
        }

    @staticmethod
    def slugify_skill_name(source_name: str) -> str:
        cleaned = re.sub(r"\W+", "_", source_name.lower()).strip("_")[:50]
        return f"job_skill_{cleaned}"

    def _load_skill_module(self, skill_name: str, force_reload: bool = False) -> Optional[Any]:
        if not force_reload and skill_name in self._loaded_skills:
            return self._loaded_skills[skill_name]
        p = self.skills_dir / f"{skill_name}.py"
        if not p.is_file():
            return None
        try:
            spec = importlib.util.spec_from_file_location(skill_name, str(p))
            if spec is None or spec.loader is None:
                return None
            mod = importlib.util.module_from_spec(spec)
            sys.modules[skill_name] = mod
            spec.loader.exec_module(mod)
            self._loaded_skills[skill_name] = mod
            return mod
        except Exception as exc:
            logger.error(f"Skill load error {skill_name}: {exc}")
            return None
