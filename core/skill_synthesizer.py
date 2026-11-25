# File: core/skill_synthesizer.py
# Version: 0.9.0 | Release Date: 2026-11-24
# EN: Modular System 2 code synthesizer & SkillCrystallizer runner with progressive
#     multi-attempt escalation (1..5+ steps from simple/deterministic to deep reasoner).
# DE: Modularer System-2-Code-Synthesizer mit mehrstufiger Eskalation (1..5+ Versuche).

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable, Dict, Optional, Set

from bs4 import BeautifulSoup

from core.escalation_config import EscalationAttemptConfig, System2EscalationPolicy
from pycognition.skills import ExecutionScope, SkillCapability, SkillCrystallizer

logger = logging.getLogger("AcademicJobHarvester.SkillSynthesizer")


class ProgressiveSkillSynthesizer:
    def __init__(
        self,
        crystallizer: SkillCrystallizer,
        policy: System2EscalationPolicy,
        allowed_modules: Set[str],
        llm_caller: Callable[[str, str, str, float], Awaitable[str]],
        on_llm_call: Optional[Callable[[], None]] = None,
        on_synthesized: Optional[Callable[[], None]] = None,
    ) -> None:
        self.crystallizer = crystallizer
        self.policy = policy
        self.allowed_modules = set(allowed_modules)
        self.llm_caller = llm_caller
        self.on_llm_call = on_llm_call
        self.on_synthesized = on_synthesized

    async def synthesize_with_escalation(
        self,
        skill_name: str,
        source_name: str,
        url: str,
        html: str,
        deepseek_api_key: str,
        current_code: Optional[str] = None,
        diagnostic_report: str = "",
        baseline_valid_count: int = 0,
    ) -> Dict[str, Any]:
        capability = SkillCapability(
            skill_name=skill_name,
            allowed_scope=ExecutionScope.PARSE_DATA,
            allowed_modules=set(self.allowed_modules),
            required_entrypoint="extract",
        )
        last_reason = "no attempts executed"
        feedback = diagnostic_report or "Initial synthesis"

        for step in self.policy.steps:
            code = await self._generate_candidate_for_step(
                step=step,
                source_name=source_name,
                url=url,
                html=html,
                deepseek_api_key=deepseek_api_key,
                current_code=current_code,
                feedback=feedback,
            )
            if not code:
                last_reason = f"step {step.attempt} ({step.model or step.mode}) empty code"
                continue

            cryst_res = self.crystallizer.crystallize(
                source_code=code,
                capability=capability,
                sample_args=(html, url),
                target_uri=url,
                baseline_valid_count=baseline_valid_count,
                max_allowed_corruption=0.25,
                source_label=source_name,
            )
            if cryst_res.accepted:
                if self.on_synthesized:
                    self.on_synthesized()
                return {
                    "accepted": True,
                    "attempt": step.attempt,
                    "total_attempts": len(self.policy.steps),
                    "model": step.model or step.mode,
                    "reason": "crystallized",
                }

            last_reason = f"step {step.attempt} ({step.model or step.mode}) rejected: {cryst_res.reason}"
            feedback = f"Previous attempt {step.attempt} failed: {cryst_res.reason}"
            current_code = code

        return {
            "accepted": False,
            "attempt": len(self.policy.steps),
            "total_attempts": len(self.policy.steps),
            "model": self.policy.steps[-1].model if self.policy.steps else "none",
            "reason": last_reason,
        }

    async def _generate_candidate_for_step(
        self,
        step: EscalationAttemptConfig,
        source_name: str,
        url: str,
        html: str,
        deepseek_api_key: str,
        current_code: Optional[str],
        feedback: str,
    ) -> str:
        if step.mode != "llm" or not deepseek_api_key or not step.model:
            return self.build_deterministic_extractor_code(source_name=source_name, seed_url=url)

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "svg", "noscript", "iframe", "meta", "link"]):
            tag.decompose()
        root = soup.find("main") or soup.find("body") or soup
        distilled_dom = re.sub(r"\n\s*\n+", "\n", str(root))[: step.dom_char_limit]

        if self.on_llm_call:
            self.on_llm_call()

        prev_block = f"Previous code:\n```python\n{current_code}\n```\n" if current_code else ""
        sys_prompt = (
            "You are the System 2 Cognitive Architect of PyCognition. Write a deterministic Python "
            "extractor for academic/university job postings using BeautifulSoup and urllib.parse.urljoin. "
            "Define `def extract(html: str, base_url: str) -> list[dict]:`. Allowed imports ONLY: "
            "`bs4`, `re`, `json`, `typing`, `datetime`, `urllib.parse`, `html`. NEVER import `os`, `sys`, "
            "`subprocess`, `requests`, `httpx`, or `pathlib`. Output ONLY Python code in ```python ... ```."
        )
        usr_prompt = (
            f"Source: {source_name}\nURL: {url}\nAttempt: {step.attempt}\n"
            f"Feedback: {feedback}\n{prev_block}DOM:\n```html\n{distilled_dom}\n```"
        )
        raw_reply = await self.llm_caller(step.model, sys_prompt, usr_prompt, step.temperature)
        m = re.search(r"```python\s*(.*?)```", raw_reply, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else ""

    @staticmethod
    def build_deterministic_extractor_code(source_name: str, seed_url: str) -> str:
        safe_source = source_name.replace('"', "'")
        safe_seed = seed_url.replace('"', "'")
        return (
            f"# Crystallized System 1 Job Skill for {safe_source}\n"
            "import re\n"
            "from urllib.parse import urljoin, urlparse\n"
            "from bs4 import BeautifulSoup\n\n"
            "POSTING_RE = re.compile(r\"(/job/|/jobs/[^/]+|/stellenangebote/[^/]+|/stelle/[^/]+|/stellenangebot/|/karriere/[^/]+|/vacanc|/position/|/offene-stellen/[^/]+|stellenangebote/.*\\.pdf$)\", re.IGNORECASE)\n"
            "NOISE_RE = re.compile(r\"(/impressum|/datenschutz|/kontakt|/ueber-uns|/studium|/lehre|/presse|/aktuelles|/forschung/?$)\", re.IGNORECASE)\n"
            "GRADE_RE = re.compile(r\"\\b(TV-?L\\s*E?\\s*1[345]|TVöD\\s*E?\\s*1[345]|E\\s*1[345]|W\\s*[123]|LfbA)\\b\", re.IGNORECASE)\n"
            "WORKLOAD_RE = re.compile(r\"\\b(100\\s*%|vollzeit|75\\s*%|50\\s*%|80\\s*%|teilzeit)\\b\", re.IGNORECASE)\n"
            "DEADLINE_RE = re.compile(r\"(?:bewerbungsfrist|frist|bis zum|deadline)[:\\s]*([0-3]?\\d\\.[01]?\\d\\.20\\d{2}|20\\d{2}-[01]\\d-[0-3]\\d)\", re.IGNORECASE)\n\n"
            f"def extract(html: str, base_url: str = \"{safe_seed}\") -> list[dict]:\n"
            "    soup = BeautifulSoup(html, \"html.parser\")\n"
            "    results = []\n"
            "    seen = set()\n"
            "    base_clean = base_url.rstrip(\"/\").lower()\n"
            "    for a in soup.find_all(\"a\", href=True):\n"
            "        raw_href = str(a[\"href\"]).strip()\n"
            "        if not raw_href or raw_href.startswith((\"#\", \"javascript:\", \"mailto:\", \"tel:\")): continue\n"
            "        full_url = urljoin(base_url, raw_href).split(\"#\")[0]\n"
            "        if full_url in seen or full_url.rstrip(\"/\").lower() == base_clean: continue\n"
            "        parsed = urlparse(full_url)\n"
            "        if parsed.scheme not in (\"http\", \"https\") or not parsed.path or parsed.path in (\"/\", \"/de\", \"/de/\"): continue\n"
            "        if NOISE_RE.search(parsed.path): continue\n"
            "        path_q = f\"{parsed.path}?{parsed.query}\" if parsed.query else parsed.path\n"
            "        if not POSTING_RE.search(path_q): continue\n"
            "        txt = re.sub(r\"\\s+\", \" \", a.get_text(\" \", strip=True)).strip()\n"
            "        txt = re.sub(r\"\\s*\\(PDF[^)]*\\)\\s*$\", \"\", txt, flags=re.IGNORECASE).strip()\n"
            "        if len(txt) < 10 or len(txt) > 220: continue\n"
            "        parent = a.find_parent([\"tr\", \"li\", \"article\", \"div\"])",
            "        ctx = re.sub(r\"\\s+\", \" \", parent.get_text(\" \", strip=True)).strip() if parent else txt\n"
            "        combined = f\"{txt} {ctx}\"\n"
            "        gm = GRADE_RE.search(combined)\n"
            "        wm = WORKLOAD_RE.search(combined)\n"
            "        dm = DEADLINE_RE.search(combined)\n"
            "        seen.add(full_url)\n"
            "        results.append({\n"
            "            \"title\": txt,\n"
            "            \"link\": full_url,\n"
            f"            \"institution\": \"{safe_source}\",\n"
            "            \"pay_grade\": re.sub(r\"\\s+\", \" \", gm.group(0)).upper() if gm else \"TV-L / Tarif\",\n"
            "            \"workload\": re.sub(r\"\\s+\", \" \", wm.group(0)).upper() if wm else \"Siehe Ausschreibung\",\n"
            "            \"deadline\": dm.group(1) if dm else \"Siehe Ausschreibung\",\n"
            "            \"description\": ctx[:450],\n"
            "        })\n"
            "    return results\n"
        )
