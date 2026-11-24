# File: admin_app.py
# Version: 0.5.0 | Release Date: 2026-11-24
# EN: Academic Job Harvester v0.5.0 — Adds one-click Anschreiben (DE) and
#     Cover Letter (EN) draft generation tailored to each vacancy and uploaded CV.
# DE: Academic Job Harvester v0.5.0 — Ergänzt die Generierung von Anschreiben (DE)
#     und Cover Letter (EN) passend zur ausgewählten Stelle und zum Lebenslauf.

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import httpx
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

_LOCAL_PYCOGNITION_SRC = Path(r"C:\Entwiklung\pycognition\src")
if _LOCAL_PYCOGNITION_SRC.exists() and str(_LOCAL_PYCOGNITION_SRC) not in sys.path:
    sys.path.insert(0, str(_LOCAL_PYCOGNITION_SRC))

try:
    import pypdf
except ImportError:
    pypdf = None  # type: ignore

try:
    import docx
except ImportError:
    docx = None  # type: ignore

try:
    from pycognition.temporal import (
        AliasMemoryStore,
        EntityNode,
        EntityObservation,
        NeuroSymbolicEntityResolver,
        ProactiveHorizonPlanner,
        ResolutionTrack,
        TemporalEventGraph,
    )
    _HAS_PYCOGNITION_TEMPORAL = True
except ImportError:
    _HAS_PYCOGNITION_TEMPORAL = False

from core.cognitive_job_engine import CognitiveJobSkillEngine

try:
    from adapters.bundesagentur_adapter import BundesagenturJobAdapter
except ImportError:
    BundesagenturJobAdapter = None  # type: ignore

try:
    from adapters.euraxess_nds_adapter import EURAXESSAndNiedersachsenAdapter
except ImportError:
    EURAXESSAndNiedersachsenAdapter = None  # type: ignore


app = FastAPI(title="Academic Job Harvester — PyCognition System 1 / System 2 Center", version="0.5.0")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CV_STATE_PATH = DATA_DIR / "active_cv_profile.json"
JOBS_CACHE_PATH = DATA_DIR / "harvested_jobs.json"


cognitive_engine = CognitiveJobSkillEngine(base_dir=BASE_DIR, data_dir=DATA_DIR)


class AcademicTemporalBridge:
    def __init__(self, data_dir: Path) -> None:
        self.enabled = _HAS_PYCOGNITION_TEMPORAL
        self.last_canonical_count = 0
        self.last_aliases_merged = 0
        self.last_er_llm_calls = 0
        self.last_planned_actions = 0

        if self.enabled:
            self.graph = TemporalEventGraph(data_dir / "academic_jobs_temporal.db")
            self.alias_store = AliasMemoryStore(data_dir / "academic_jobs_aliases.db")
            self.resolver = NeuroSymbolicEntityResolver(
                alias_store=self.alias_store,
                high_confidence_threshold=0.90,
                low_confidence_threshold=0.42,
                semantic_arbiter=None,
            )
            self.planner = ProactiveHorizonPlanner()

    async def ingest_jobs(self, jobs: List[Dict[str, Any]], source_name: str) -> List[Dict[str, Any]]:
        if not jobs:
            self.last_canonical_count = 0
            return []
        if not self.enabled:
            self.last_canonical_count = len(jobs)
            return jobs

        now_utc = datetime.now(timezone.utc)
        existing_nodes = self.graph.list_nodes()
        node_index: Dict[str, EntityNode] = {n.canonical_id: n for n in existing_nodes}

        merged_in_batch = 0
        llm_in_batch = 0
        transitions = []
        canonical_map: Dict[str, Dict[str, Any]] = {}

        for job in jobs:
            title = str(job.get("title") or "").strip()
            url = str(job.get("url") or job.get("link") or "").strip()
            if not title or not url.startswith(("https://", "http://")):
                continue

            deadline_raw = str(job.get("deadline") or "")
            iso_m = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", deadline_raw)
            deadline_iso = iso_m.group(1) if iso_m else None

            attrs = {
                "funder": str(job.get("institution") or source_name),
                "pay_grade": str(job.get("pay_grade") or ""),
                "workload": str(job.get("workload") or ""),
                "location": str(job.get("location") or ""),
                "kennziffer": str(job.get("kennziffer") or ""),
            }

            decision = await self.resolver.resolve_async(
                title=title,
                source_id=str(job.get("source_portal") or source_name),
                raw_uri=url,
                existing_nodes=list(node_index.values()),
                deadline_iso=deadline_iso,
                attributes=attrs,
            )
            if decision.llm_invoked:
                llm_in_batch += 1
            if not decision.is_new_entity and decision.track_used in (
                ResolutionTrack.CACHE_HIT,
                ResolutionTrack.SYSTEM1_LEXICAL,
                ResolutionTrack.SYSTEM2_SEMANTIC,
            ):
                merged_in_batch += 1

            obs = EntityObservation(
                canonical_id=decision.canonical_id,
                source_id=str(job.get("source_portal") or source_name),
                raw_uri=url,
                title=title,
                attributes=attrs,
                deadline_iso=deadline_iso,
                valid_time=now_utc,
                transaction_time=now_utc,
            )
            edge = self.graph.ingest_observation(obs, expiring_window_days=14)
            transitions.append(edge)

            updated = self.graph.get_node(decision.canonical_id)
            if updated is not None:
                node_index[decision.canonical_id] = updated

            enriched = dict(job)
            enriched["canonical_id"] = decision.canonical_id
            enriched["is_duplicate_alias"] = not decision.is_new_entity
            if decision.canonical_id in canonical_map:
                prev = canonical_map[decision.canonical_id]
                merged_tags = list(dict.fromkeys([*(prev.get("tags") or []), *(enriched.get("tags") or [])]))
                enriched["tags"] = merged_tags[:14]
                if prev.get("source_portal") and prev.get("source_portal") != enriched.get("source_portal"):
                    enriched["source_portal"] = f"{prev['source_portal']} + {enriched['source_portal']}"
            canonical_map[decision.canonical_id] = enriched

        planned = self.planner.plan_from_transitions(transitions, graph=self.graph, now=now_utc)
        self.last_canonical_count = len(self.graph.list_nodes())
        self.last_aliases_merged = merged_in_batch
        self.last_er_llm_calls = llm_in_batch
        self.last_planned_actions = len(planned)
        return list(canonical_map.values())


temporal_bridge = AcademicTemporalBridge(DATA_DIR)


def _load_jobs() -> List[Dict[str, Any]]:
    if not JOBS_CACHE_PATH.is_file():
        return []
    try:
        loaded = json.loads(JOBS_CACHE_PATH.read_text(encoding="utf-8"))
        if not isinstance(loaded, list):
            return []
        valid: List[Dict[str, Any]] = []
        for item in loaded:
            if not isinstance(item, dict):
                continue
            u = str(item.get("url") or item.get("link") or "").strip()
            if u.startswith(("https://", "http://")):
                item["url"] = u
                item["link"] = u
                valid.append(item)
        return valid
    except Exception:
        return []


def _save_jobs(jobs: List[Dict[str, Any]]) -> None:
    JOBS_CACHE_PATH.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")


HARVEST_STATE: Dict[str, Any] = {
    "running": False,
    "system2_busy": False,
    "progress": 100,
    "active_source": "Готов · Нажмите «▶ ЗАПУСТИТЬ СБОР (СИСТЕМА 1)» или «🧠 ОБУЧИТЬ СИСТЕМУ 2»",
    "last_run": "2026-11-24 09:10 CET",
    "raw_collected": 0,
    "unique_canonical": 0,
    "last_api_hits": 0,
    "last_errors": [],
}


class VoiceQueryRequest(BaseModel):
    transcript: str


class TeachSourceRequest(BaseModel):
    url: str
    name: str = ""


class CoverLetterRequest(BaseModel):
    job_id: str
    lang: str = "de"


KNOWN_SKILL_LEXICON = [
    "python", "dspace", "dspace-cris", "cris", "kdsf", "solr", "sqlite", "postgresql",
    "rest api", "docker", "typescript", "angular", "rdm", "fdm", "datacite", "doi",
    "dms", "sap", "ai", "llm", "knowledge graph", "forschungsförderung", "eu horizon",
    "projektmanagement", "innovation management", "transfer", "open source", "automation",
    "analytics", "german b2", "english", "forschung", "wissenschaft", "tv-l e13", "tv-l e14",
    "universität vechta", "niedersachsen", "research",
]


def _extract_text_from_upload(filename: str, content: bytes) -> str:
    lower = filename.lower()
    if lower.endswith(".pdf") and pypdf is not None:
        reader = pypdf.PdfReader(io.BytesIO(content))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if lower.endswith(".docx") and docx is not None:
        doc = docx.Document(io.BytesIO(content))
        return "\n".join(p.text for p in doc.paragraphs)
    return content.decode("utf-8", errors="ignore")


def _build_cv_profile(filename: str, raw_text: str) -> Dict[str, Any]:
    clean = re.sub(r"\s+", " ", raw_text).strip()
    lower = clean.lower()
    detected_skills = [kw for kw in KNOWN_SKILL_LEXICON if kw in lower]
    if not detected_skills:
        words = re.findall(r"[A-Za-zÄÖÜäöüß\-]{4,}", lower)
        freq: Dict[str, int] = {}
        for w in words:
            freq[w] = freq.get(w, 0) + 1
        detected_skills = [k for k, _ in sorted(freq.items(), key=lambda x: -x[1])[:12]]

    profile = {
        "filename": filename,
        "uploaded_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "char_count": len(clean),
        "skills": detected_skills,
        "preview": clean[:240],
    }
    CV_STATE_PATH.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    return profile


def _load_cv_profile() -> Dict[str, Any]:
    if CV_STATE_PATH.is_file():
        try:
            return json.loads(CV_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "filename": "Default Profile",
        "uploaded_at": "2026-11-24",
        "char_count": 0,
        "skills": ["python", "dspace-cris", "kdsf", "rest api", "rdm", "ai", "innovation management"],
        "preview": "Перетащите PDF/DOCX резюме сюда для расчёта совпадений.",
    }


def _score_jobs(jobs: List[Dict[str, Any]], cv_skills: List[str], query_filter: str = "") -> List[Dict[str, Any]]:
    cv_set = {s.lower() for s in cv_skills}
    q = query_filter.lower().strip()
    enriched: List[Dict[str, Any]] = []

    for job in jobs:
        job_tags = [str(t).lower() for t in (job.get("tags") or [])]
        matched = [t for t in job_tags if t in cv_set]
        missing = [t for t in job_tags if t not in cv_set]
        score = int(round((len(matched) / max(1, len(job_tags))) * 100))

        haystack = " ".join([
            str(job.get("title") or ""),
            str(job.get("institution") or ""),
            str(job.get("location") or ""),
            str(job.get("pay_grade") or ""),
            str(job.get("workload") or ""),
            str(job.get("contract") or ""),
            str(job.get("source_portal") or ""),
            *job_tags,
        ]).lower()

        if q:
            tokens = [tok for tok in re.split(r"[\s,;]+", q) if len(tok) >= 2]
            if tokens and not any(tok in haystack for tok in tokens):
                continue

        item = dict(job)
        item["match_score"] = min(99, max(20, score))
        item["matched_tags"] = matched
        item["missing_tags"] = missing[:3]
        enriched.append(item)

    enriched.sort(key=lambda x: -x["match_score"])
    return enriched


def _parse_voice_intent(transcript: str) -> Dict[str, Any]:
    text = transcript.strip()
    lower = text.lower()
    extracted_filters: List[str] = []

    for grade in ("e13", "e14", "e15", "tv-l", "tvöd", "w1", "w2", "w3"):
        if grade in lower:
            extracted_filters.append(grade.upper())
    if "100" in lower or "полн" in lower or "vollzeit" in lower:
        extracted_filters.append("100%")
    if "unbefristet" in lower or "бессроч" in lower:
        extracted_filters.append("Unbefristet")
    for region in ("niedersachsen", "нижн", "hannover", "oldenburg", "bremen", "vechta", "euraxess", "remote", "удален", "удалён", "hybrid"):
        if region in lower:
            extracted_filters.append(region)

    if not extracted_filters:
        extracted_filters = [w for w in re.findall(r"[A-Za-zА-Яа-яЁёÄÖÜäöüß0-9\-]{3,}", text) if len(w) >= 3][:4]

    return {
        "transcript": text,
        "filter_query": " ".join(extracted_filters),
        "detected_chips": extracted_filters,
    }


def _build_letter_draft(job: Dict[str, Any], cv: Dict[str, Any], lang: str) -> str:
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

    if lang.lower().startswith("en"):
        return (
            f"Subject: Application for {title}"
            f"{f' (Ref.: {kennziffer})' if kennziffer else ''}\n\n"
            f"Dear Hiring Committee at {institution},\n\n"
            f"I am writing to express my strong interest in the position of \"{title}\" "
            f"({location}, {pay_grade}, {workload}). Based on my background profile ({cv_name}), "
            f"I bring hands-on experience in {skills_str}, combining software engineering, "
            f"research data/information architecture, and structured project coordination.\n\n"
            f"I am eager to contribute to {institution}'s research and innovation goals and look forward "
            f"to discussing how my technical and organizational expertise aligns with this role.\n"
            f"{f'Posting reference: {url}\n' if url else ''}\n"
            f"Sincerely,\n[Your Name]"
        )

    return (
        f"Bewerbung um die Stelle als „{title}“"
        f"{f' (Kennziffer: {kennziffer})' if kennziffer else ''}\n\n"
        f"Sehr geehrte Damen und Herren,\n\n"
        f"mit großem Interesse bewerbe ich mich auf die ausgeschriebene Position „{title}“ "
        f"bei {institution} ({location}, Vergütung: {pay_grade}, Umfang: {workload}). "
        f"Auf Basis meines hinterlegten Profils ({cv_name}) bringe ich fundierte Praxis in den Bereichen "
        f"{skills_str} mit und verbinde technische Systementwicklung mit Forschungsorganisation und Transfer.\n\n"
        f"Gerne unterstütze ich {institution} mit einer strukturierten, lösungsorientierten Arbeitsweise "
        f"und freue mich auf ein persönliches oder digitales Gespräch.\n"
        f"{f'Ausschreibungslink: {url}\n' if url else ''}\n"
        f"Mit freundlichen Grüßen\n[Ihr Name]"
    )


async def _polish_letter_via_deepseek(draft: str, lang: str) -> str:
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


async def _execute_system1_cycle() -> None:
    HARVEST_STATE["running"] = True
    HARVEST_STATE["progress"] = 20
    HARVEST_STATE["active_source"] = "Система 1: запуск кристаллизованных скиллов (skills_registry/)..."
    HARVEST_STATE["last_errors"] = []

    collected: List[Dict[str, Any]] = []

    s1_records = await cognitive_engine.run_system1_harvest()
    collected.extend(s1_records)

    HARVEST_STATE["progress"] = 55
    HARVEST_STATE["active_source"] = "Tier-1 Адаптеры: Bundesagentur API & EURAXESS..."

    if BundesagenturJobAdapter is not None:
        try:
            ba = BundesagenturJobAdapter(size=25, require_academic_match=True)
            b_batch = await ba.fetch_changes()
            HARVEST_STATE["last_api_hits"] = int(b_batch.metadata.get("api_raw_hits", 0))
            collected.extend(b_batch.records)
        except Exception as exc:
            HARVEST_STATE["last_errors"].append(str(exc))

    if EURAXESSAndNiedersachsenAdapter is not None:
        try:
            nds = EURAXESSAndNiedersachsenAdapter()
            n_batch = await nds.fetch_changes()
            collected.extend(n_batch.records)
        except Exception as exc:
            HARVEST_STATE["last_errors"].append(str(exc))

    dedup_by_url: Dict[str, Dict[str, Any]] = {}
    for rec in collected:
        u = str(rec.get("url") or "").strip()
        if u.startswith(("https://", "http://")):
            dedup_by_url[u] = rec

    real_jobs = list(dedup_by_url.values())
    HARVEST_STATE["progress"] = 85
    HARVEST_STATE["active_source"] = "PyCognition TemporalEventGraph & EntityResolver..."

    resolved_jobs = await temporal_bridge.ingest_jobs(
        real_jobs,
        source_name="PyCognition System 1 + Tier-1 Adapters",
    )
    _save_jobs(resolved_jobs)

    HARVEST_STATE["raw_collected"] = len(collected) + HARVEST_STATE["last_api_hits"]
    HARVEST_STATE["unique_canonical"] = len(resolved_jobs)
    HARVEST_STATE["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    HARVEST_STATE["progress"] = 100
    HARVEST_STATE["active_source"] = (
        f"Готово · Собрано {len(resolved_jobs)} вакансий "
        f"(Скиллов Системы 1: {cognitive_engine.stats['system1_skills_count']}, "
        f"ср. время: {cognitive_engine.stats['system1_avg_ms']} мс)"
    )
    HARVEST_STATE["running"] = False


async def _execute_system2_discovery_cycle() -> None:
    HARVEST_STATE["system2_busy"] = True
    HARVEST_STATE["active_source"] = "Система 2 (HRK / Hochschulkompass): поиск вузов и синтез скиллов..."
    try:
        await cognitive_engine.discover_and_build_adapters(max_new_sources=6)
        await _execute_system1_cycle()
    finally:
        HARVEST_STATE["system2_busy"] = False


@app.get("/api/dashboard")
async def get_dashboard_state(q: str = "") -> JSONResponse:
    cv = _load_cv_profile()
    all_jobs = _load_jobs()
    jobs = _score_jobs(all_jobs, cv.get("skills", []), query_filter=q)
    skills_list = cognitive_engine.list_skills_status()

    pay_dist: Dict[str, int] = {}
    horizon_dist: Dict[str, int] = {"H1": 0, "H2": 0, "H3": 0}
    for j in jobs:
        pg = str(j.get("pay_grade") or "TV-L / Tarif")
        pay_dist[pg] = pay_dist.get(pg, 0) + 1
        h = str(j.get("horizon") or "H3")
        horizon_dist[h] = horizon_dist.get(h, 0) + 1

    high_match_count = sum(1 for j in jobs if j["match_score"] >= 60)

    return JSONResponse({
        "harvest": HARVEST_STATE,
        "cognition_stats": cognitive_engine.stats,
        "system1_skills": skills_list,
        "temporal_stats": {
            "enabled": temporal_bridge.enabled,
            "canonical_entities": len(all_jobs),
            "aliases_merged": temporal_bridge.last_aliases_merged,
            "er_llm_calls": temporal_bridge.last_er_llm_calls,
            "planned_actions": temporal_bridge.last_planned_actions,
        },
        "cv_profile": cv,
        "funnel": {
            "raw": max(HARVEST_STATE["raw_collected"], len(all_jobs)),
            "unique": len(all_jobs),
            "matched": high_match_count,
        },
        "pay_distribution": pay_dist,
        "horizon_distribution": horizon_dist,
        "jobs": jobs[:50],
    })


@app.post("/api/harvest/toggle")
async def toggle_harvest() -> JSONResponse:
    if HARVEST_STATE["running"]:
        HARVEST_STATE["running"] = False
        HARVEST_STATE["active_source"] = "Остановлено пользователем"
        return JSONResponse(HARVEST_STATE)

    asyncio.create_task(_execute_system1_cycle())
    return JSONResponse(HARVEST_STATE)


@app.post("/api/system2/discover")
async def trigger_system2_discovery() -> JSONResponse:
    if not HARVEST_STATE["system2_busy"]:
        asyncio.create_task(_execute_system2_discovery_cycle())
    return JSONResponse({"status": "started", "stats": cognitive_engine.stats})


@app.post("/api/system2/teach")
async def teach_system2_source(req: TeachSourceRequest) -> JSONResponse:
    res = await cognitive_engine.teach_new_source(url=req.url, name=req.name)
    await _execute_system1_cycle()
    return JSONResponse(res)


@app.post("/api/cover-letter")
async def generate_cover_letter(req: CoverLetterRequest) -> JSONResponse:
    cv = _load_cv_profile()
    jobs = _load_jobs()
    job = next((item for item in jobs if str(item.get("id")) == req.job_id), None)
    if not job:
        return JSONResponse({"status": "error", "letter": "Вакансия не найдена в текущем каталоге."}, status_code=404)

    base_draft = _build_letter_draft(job=job, cv=cv, lang=req.lang)
    final_text = await _polish_letter_via_deepseek(draft=base_draft, lang=req.lang)
    return JSONResponse({
        "status": "ok",
        "job_title": job.get("title"),
        "institution": job.get("institution"),
        "lang": req.lang,
        "letter": final_text,
    })


@app.post("/api/cv/upload")
async def upload_cv(file: UploadFile = File(...)) -> JSONResponse:
    content = await file.read()
    text = _extract_text_from_upload(file.filename or "resume.txt", content)
    profile = _build_cv_profile(file.filename or "resume.txt", text)
    return JSONResponse({"status": "ok", "profile": profile})


@app.post("/api/voice/parse")
async def parse_voice_command(req: VoiceQueryRequest) -> JSONResponse:
    return JSONResponse(_parse_voice_intent(req.transcript))


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Academic Job Harvester · PyCognition System 1 / System 2</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
  <style>
    :root {
      --bg: #0b1120;
      --card: #131c31;
      --border: #243354;
      --text: #f1f5f9;
      --muted: #94a3b8;
      --green: #10b981;
      --blue: #3b82f6;
      --amber: #f59e0b;
      --purple: #8b5cf6;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      padding: 20px;
    }
    .header {
      display: flex; justify-content: space-between; align-items: center;
      margin-bottom: 18px; padding-bottom: 14px; border-bottom: 1px solid var(--border);
    }
    .brand { display: flex; align-items: center; gap: 12px; }
    .brand-badge {
      background: linear-gradient(135deg, var(--blue), var(--purple));
      padding: 8px 14px; border-radius: 12px; font-weight: 800; font-size: 17px;
    }
    .big-controls {
      display: grid; grid-template-columns: 1.1fr 1.15fr 1fr 1fr; gap: 14px; margin-bottom: 20px;
    }
    .control-tile {
      background: var(--card); border: 2px solid var(--border); border-radius: 18px;
      padding: 16px; display: flex; flex-direction: column; justify-content: space-between;
    }
    .btn-giant {
      width: 100%; padding: 15px 12px; border: none; border-radius: 14px;
      font-size: 15px; font-weight: 800; color: #fff; cursor: pointer;
      display: flex; align-items: center; justify-content: center; gap: 8px;
      box-shadow: 0 8px 20px rgba(0,0,0,0.3);
    }
    .btn-start { background: linear-gradient(135deg, #059669, #10b981); }
    .btn-stop  { background: linear-gradient(135deg, #dc2626, #ef4444); }
    .btn-sys2  { background: linear-gradient(135deg, #7c3aed, #8b5cf6); }
    .btn-mic   { background: linear-gradient(135deg, #2563eb, #3b82f6); }
    .btn-mic.listening { background: linear-gradient(135deg, #d97706, #f59e0b); animation: pulse 1.2s infinite; }
    @keyframes pulse { 0% { transform: scale(1); } 50% { transform: scale(1.02); } 100% { transform: scale(1); } }

    .progress-bar {
      height: 8px; background: #1e293b; border-radius: 999px; overflow: hidden; margin-top: 10px;
    }
    .progress-fill { height: 100%; background: var(--green); width: 100%; transition: width 0.4s; }

    .drop-zone {
      border: 2px dashed #475569; border-radius: 14px; padding: 12px; text-align: center;
      cursor: pointer; background: rgba(30, 41, 59, 0.45); transition: 0.2s;
    }
    .drop-zone.dragover { border-color: var(--green); background: rgba(16, 185, 129, 0.15); }
    .chip-row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
    .chip {
      font-size: 11px; padding: 3px 9px; border-radius: 999px;
      background: rgba(59, 130, 246, 0.2); border: 1px solid rgba(59, 130, 246, 0.45); color: #93c5fd;
    }
    .chip-s1 {
      font-size: 11px; padding: 4px 10px; border-radius: 999px;
      background: rgba(16, 185, 129, 0.16); border: 1px solid rgba(16, 185, 129, 0.45); color: #6ee7b7;
    }

    .charts-grid {
      display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 20px;
    }
    .chart-card {
      background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 16px;
    }
    .chart-card h3 { font-size: 13px; color: var(--muted); margin-bottom: 10px; text-transform: uppercase; letter-spacing: 0.04em; }
    .chart-wrap { height: 180px; position: relative; }

    .skills-bar {
      background: var(--card); border: 1px solid var(--border); border-radius: 16px;
      padding: 14px 18px; margin-bottom: 20px;
    }

    .jobs-section {
      background: var(--card); border: 1px solid var(--border); border-radius: 18px; padding: 18px;
    }
    .job-row {
      display: grid; grid-template-columns: 84px 1fr auto; gap: 16px; align-items: center;
      padding: 14px; border-bottom: 1px solid var(--border);
    }
    .job-row:last-child { border-bottom: none; }
    .job-actions { display: flex; flex-direction: column; gap: 8px; min-width: 155px; }
    .btn-open {
      background: #1e293b; color: #f8fafc; padding: 9px 12px; border-radius: 10px;
      text-decoration: none; font-size: 12px; font-weight: 700; border: 1px solid var(--border); text-align: center;
    }
    .btn-letter {
      background: rgba(59, 130, 246, 0.18); color: #93c5fd; padding: 8px 10px; border-radius: 10px;
      border: 1px solid rgba(59, 130, 246, 0.45); font-size: 11px; font-weight: 700; cursor: pointer;
    }
    .score-badge {
      width: 74px; height: 74px; border-radius: 16px; display: flex; flex-direction: column;
      align-items: center; justify-content: center; font-weight: 800; font-size: 20px;
      background: rgba(16, 185, 129, 0.16); border: 2px solid var(--green); color: #6ee7b7;
    }
    .score-badge.mid { background: rgba(245, 158, 11, 0.16); border-color: var(--amber); color: #fcd34d; }
    .score-badge span { font-size: 10px; font-weight: 600; color: var(--muted); }
    .meta-pills { display: flex; flex-wrap: wrap; gap: 8px; margin: 6px 0; font-size: 12px; }
    .pill { background: #1e293b; padding: 3px 10px; border-radius: 8px; color: #cbd5e1; }
    .pill-src { background: rgba(139, 92, 246, 0.2); border: 1px solid rgba(139, 92, 246, 0.45); color: #ddd6fe; padding: 3px 10px; border-radius: 8px; }
    .tag-ok { background: rgba(16, 185, 129, 0.2); color: #6ee7b7; padding: 2px 8px; border-radius: 6px; font-size: 11px; }
    .tag-miss { background: rgba(245, 158, 11, 0.18); color: #fcd34d; padding: 2px 8px; border-radius: 6px; font-size: 11px; }
    .voice-input {
      width: 100%; margin-top: 8px; padding: 8px 10px; border-radius: 10px;
      border: 1px solid var(--border); background: #0f172a; color: var(--text); font-size: 13px;
    }
    .teach-row { display: flex; gap: 6px; margin-top: 8px; }
    .btn-mini {
      background: #334155; color: #fff; border: none; border-radius: 9px;
      padding: 0 12px; font-weight: 700; cursor: pointer; font-size: 12px;
    }
    .empty-state { padding: 26px; text-align: center; color: var(--muted); font-size: 14px; }

    /* Modal for Anschreiben / Cover Letter */
    .modal-backdrop {
      display: none; position: fixed; inset: 0; background: rgba(2, 6, 23, 0.78);
      align-items: center; justify-content: center; z-index: 999; padding: 20px;
    }
    .modal-backdrop.open { display: flex; }
    .modal-box {
      background: var(--card); border: 2px solid var(--border); border-radius: 18px;
      width: 100%; max-width: 720px; padding: 20px; box-shadow: 0 24px 48px rgba(0,0,0,0.6);
    }
    .letter-area {
      width: 100%; min-height: 290px; margin: 12px 0; padding: 14px; border-radius: 12px;
      border: 1px solid var(--border); background: #0b1120; color: var(--text);
      font-family: inherit; font-size: 13px; line-height: 1.5; resize: vertical;
    }
  </style>
</head>
<body>
  <div class="header">
    <div class="brand">
      <div class="brand-badge">PyCognition v1.3 · S1/S2</div>
      <div>
        <h1 style="font-size:19px;">Academic Job Harvester · Когнитивный пульт управления</h1>
        <div id="temporalMeta" style="font-size:12px; color:var(--muted);">Система 1 + Система 2 + Генератор Anschreiben / Cover Letter</div>
      </div>
    </div>
    <div id="cognitionBadge" style="font-size:12px; color:#c4b5fd; font-weight:600;">⚡ Скиллов S1: 0 · Синтезов S2: 0</div>
  </div>

  <div class="big-controls">
    <div class="control-tile">
      <div>
        <div style="font-size:11px; color:var(--muted); margin-bottom:6px;">1 · СИСТЕМА 1 (БЫСТРЫЙ СБОР)</div>
        <button id="harvestBtn" class="btn-giant btn-start" onclick="toggleHarvest()">▶ ЗАПУСТИТЬ СБОР</button>
      </div>
      <div>
        <div class="progress-bar"><div id="progressFill" class="progress-fill"></div></div>
        <div id="sourceLabel" style="font-size:11px; color:var(--muted); margin-top:6px;">Ожидание запуска</div>
      </div>
    </div>

    <div class="control-tile">
      <div>
        <div style="font-size:11px; color:var(--muted); margin-bottom:6px;">2 · СИСТЕМА 2 (HRK / ВУЗЫ / СИНТЕЗ)</div>
        <button id="sys2Btn" class="btn-giant btn-sys2" onclick="runSystem2Discovery()">🧠 НАЙТИ ИСТОЧНИКИ И АДАПТЕРЫ</button>
      </div>
      <div>
        <div class="teach-row">
          <input id="teachUrlInput" class="voice-input" style="margin-top:0;" placeholder="Вставьте URL вуза (напр. uni-muenster.de/...)" />
          <button class="btn-mini" onclick="teachNewUrl()">+ Обучить</button>
        </div>
        <div id="sys2Log" style="font-size:11px; color:#c4b5fd; margin-top:6px;">Автопоиск и AST-синтез</div>
      </div>
    </div>

    <div class="control-tile">
      <div>
        <div style="font-size:11px; color:var(--muted); margin-bottom:6px;">3 · ГОЛОСОВОЙ ФИЛЬТР</div>
        <button id="micBtn" class="btn-giant btn-mic" onclick="startVoiceSearch()">🎤 СКАЗАТЬ, ЧТО ИСКАТЬ</button>
      </div>
      <div>
        <input id="searchInput" class="voice-input" placeholder="E13, Vechta, Oldenburg, PDF..." oninput="refreshDashboard()" />
        <div id="voiceChips" class="chip-row"></div>
      </div>
    </div>

    <div class="control-tile">
      <div style="font-size:11px; color:var(--muted); margin-bottom:6px;">4 · ПЕРЕТАЩИТЕ РЕЗЮМЕ (PDF/DOCX)</div>
      <div id="dropZone" class="drop-zone" onclick="document.getElementById('cvFile').click()">
        <div style="font-size:20px;">📄⬇️</div>
        <div id="cvTitle" style="font-weight:700; font-size:13px; margin-top:3px;">Бросьте резюме сюда</div>
        <div id="cvSub" style="font-size:10px; color:var(--muted);">Рейтинг совпадений</div>
        <input id="cvFile" type="file" accept=".pdf,.docx,.txt" style="display:none" onchange="uploadCVFile(this.files[0])" />
      </div>
      <div id="cvSkills" class="chip-row"></div>
    </div>
  </div>

  <div class="skills-bar">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
      <span style="font-size:12px; font-weight:700; color:#93c5fd;">⚙️ КРИСТАЛЛИЗОВАННЫЕ АДАПТЕРЫ СИСТЕМЫ 1 (skills_registry/ · AST-проверено)</span>
      <span id="skillsSummary" style="font-size:12px; color:var(--muted);"></span>
    </div>
    <div id="skillsChips" class="chip-row"></div>
  </div>

  <div class="charts-grid">
    <div class="chart-card">
      <h3>1. Воронка дедупликации PyCognition</h3>
      <div class="chart-wrap"><canvas id="funnelChart"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>2. Тарифные группы (TV-L / TVöD / W)</h3>
      <div class="chart-wrap"><canvas id="payChart"></canvas></div>
    </div>
    <div class="chart-card">
      <h3>3. Радар дедлайнов (H1 / H2 / H3)</h3>
      <div class="chart-wrap"><canvas id="horizonChart"></canvas></div>
    </div>
  </div>

  <div class="jobs-section">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
      <h2 style="font-size:16px;">🎯 Реальные вакансии с прямыми ссылками и генератором письма</h2>
      <span id="jobsCount" style="font-size:13px; color:var(--muted);"></span>
    </div>
    <div id="jobsList"></div>
  </div>

  <!-- MODAL FOR ANSCHREIBEN / COVER LETTER -->
  <div id="letterModal" class="modal-backdrop" onclick="if(event.target===this)closeLetterModal()">
    <div class="modal-box">
      <div style="display:flex; justify-content:space-between; align-items:center;">
        <h3 id="letterModalTitle" style="font-size:16px;">✉ Черновик письма</h3>
        <button class="btn-mini" style="padding:6px 10px;" onclick="closeLetterModal()">✕ Закрыть</button>
      </div>
      <div id="letterModalSub" style="font-size:12px; color:var(--muted); margin-top:4px;"></div>
      <textarea id="letterTextArea" class="letter-area"></textarea>
      <div style="display:flex; gap:10px; justify-content:flex-end;">
        <button class="btn-letter" style="padding:10px 14px; font-size:12px;" onclick="copyLetterText()">📋 Копировать</button>
        <button class="btn-mini" style="padding:10px 14px; background:var(--green);" onclick="downloadLetterFile()">⬇ Скачать .TXT</button>
      </div>
    </div>
  </div>

  <script>
    let funnelChart, payChart, horizonChart;
    let currentLetterLang = 'de';

    async function refreshDashboard() {
      const q = document.getElementById('searchInput').value || '';
      const res = await fetch('/api/dashboard?q=' + encodeURIComponent(q));
      const data = await res.json();

      const btn = document.getElementById('harvestBtn');
      if (data.harvest.running) {
        btn.className = 'btn-giant btn-stop';
        btn.innerHTML = '⏹ СТОП (' + data.harvest.progress + '%)';
      } else {
        btn.className = 'btn-giant btn-start';
        btn.innerHTML = '▶ ЗАПУСТИТЬ СБОР';
      }
      document.getElementById('progressFill').style.width = data.harvest.progress + '%';
      document.getElementById('sourceLabel').textContent = data.harvest.active_source;

      if (data.cognition_stats) {
        const cs = data.cognition_stats;
        document.getElementById('cognitionBadge').textContent =
          `⚡ S1 Скиллов: ${cs.system1_skills_count} (${cs.system1_avg_ms} мс) · S2 Синтезов: ${cs.system2_synthesized} · S2 Ремонтов: ${cs.system2_repaired}`;
        document.getElementById('sys2Log').textContent = cs.last_system2_log || '';
      }

      if (data.temporal_stats) {
        document.getElementById('temporalMeta').textContent =
          `Уникальных вакансий: ${data.temporal_stats.canonical_entities} · Склеено дублей: ${data.temporal_stats.aliases_merged} · Горизонт H1-H3: ${data.temporal_stats.planned_actions}`;
      }

      const skills = data.system1_skills || [];
      const activeSkills = skills.filter(s => s.crystallized);
      document.getElementById('skillsSummary').textContent = `Активно ${activeSkills.length} из ${skills.length} источников`;
      document.getElementById('skillsChips').innerHTML = skills.map(s =>
        `<span class="${s.crystallized ? 'chip-s1' : 'chip'}">${s.crystallized ? '✓' : '⏳'} ${s.institution || s.name}</span>`
      ).join('');

      document.getElementById('cvTitle').textContent = '✅ ' + data.cv_profile.filename;
      document.getElementById('cvSub').textContent = data.cv_profile.preview;
      document.getElementById('cvSkills').innerHTML = (data.cv_profile.skills || [])
        .slice(0, 6).map(s => `<span class="chip">${s}</span>`).join('');

      renderCharts(data);
      renderJobs(data.jobs);
    }

    function renderCharts(data) {
      const commonOpts = { responsive: true, maintainAspectRatio: false, plugins: { legend: { labels: { color: '#cbd5e1', font: { size: 11 } } } } };

      if (funnelChart) funnelChart.destroy();
      funnelChart = new Chart(document.getElementById('funnelChart'), {
        type: 'bar',
        data: {
          labels: ['Проверено', 'Прямые ссылки', 'Подходят под CV'],
          datasets: [{
            data: [data.funnel.raw, data.funnel.unique, data.funnel.matched],
            backgroundColor: ['#3b82f6', '#8b5cf6', '#10b981'],
            borderRadius: 8
          }]
        },
        options: { ...commonOpts, plugins: { legend: { display: false } }, scales: { x: { ticks: { color: '#94a3b8' } }, y: { ticks: { color: '#94a3b8' } } } }
      });

      if (payChart) payChart.destroy();
      payChart = new Chart(document.getElementById('payChart'), {
        type: 'doughnut',
        data: {
          labels: Object.keys(data.pay_distribution).length ? Object.keys(data.pay_distribution) : ['Ожидание'],
          datasets: [{
            data: Object.values(data.pay_distribution).length ? Object.values(data.pay_distribution) : [1],
            backgroundColor: ['#10b981', '#3b82f6', '#8b5cf6', '#f59e0b', '#ec4899'],
            borderWidth: 0
          }]
        },
        options: commonOpts
      });

      if (horizonChart) horizonChart.destroy();
      horizonChart = new Chart(document.getElementById('horizonChart'), {
        type: 'bar',
        data: {
          labels: ['H1 (≤2 дн)', 'H2 (3–14 дн)', 'H3 (14+ дн)'],
          datasets: [{
            data: [data.horizon_distribution.H1, data.horizon_distribution.H2, data.horizon_distribution.H3],
            backgroundColor: ['#ef4444', '#f59e0b', '#10b981'],
            borderRadius: 8
          }]
        },
        options: { ...commonOpts, indexAxis: 'y', plugins: { legend: { display: false } }, scales: { x: { ticks: { color: '#94a3b8' } }, y: { ticks: { color: '#94a3b8' } } } }
      });
    }

    function renderJobs(jobs) {
      document.getElementById('jobsCount').textContent = `Показано вакансий: ${jobs.length}`;
      const container = document.getElementById('jobsList');
      if (!jobs.length) {
        container.innerHTML = `<div class="empty-state">Нажмите «🧠 НАЙТИ ИСТОЧНИКИ И АДАПТЕРЫ» (Система 2) или «▶ ЗАПУСТИТЬ СБОР» (Система 1).</div>`;
        return;
      }
      container.innerHTML = jobs.map(j => `
        <div class="job-row">
          <div class="score-badge ${j.match_score < 60 ? 'mid' : ''}">
            ${j.match_score}%
            <span>СОВПАДЕНИЕ</span>
          </div>
          <div>
            <div style="font-weight:700; font-size:15px;">${j.title}</div>
            <div style="font-size:13px; color:#93c5fd; margin-top:2px;">🏛 ${j.institution} · 📍 ${j.location}</div>
            <div class="meta-pills">
              <span class="pill">💶 ${j.pay_grade}</span>
              <span class="pill">⏱ ${j.workload}</span>
              <span class="pill">📄 ${j.contract}</span>
              <span class="pill">📅 ${j.deadline}</span>
              <span class="pill-src">⚙️ ${j.source_portal || 'System 1'}</span>
            </div>
            <div class="chip-row">
              ${(j.matched_tags || []).map(t => `<span class="tag-ok">✓ ${t}</span>`).join('')}
              ${(j.missing_tags || []).map(t => `<span class="tag-miss">+ ${t}</span>`).join('')}
            </div>
          </div>
          <div class="job-actions">
            <a class="btn-open" href="${j.url}" target="_blank" rel="noopener noreferrer">Открыть вакансию ↗</a>
            <button class="btn-letter" onclick="openCoverLetter('${j.id}', 'de')">✉ Anschreiben (DE)</button>
            <button class="btn-letter" onclick="openCoverLetter('${j.id}', 'en')">✉ Cover Letter (EN)</button>
          </div>
        </div>
      `).join('');
    }

    async function openCoverLetter(jobId, lang) {
      currentLetterLang = lang;
      document.getElementById('letterModalTitle').textContent =
        lang === 'en' ? '✉ Cover Letter Draft (EN)' : '✉ Anschreiben-Entwurf (DE)';
      document.getElementById('letterModalSub').textContent = 'Формируется с учётом вакансии и вашего CV...';
      document.getElementById('letterTextArea').value = '';
      document.getElementById('letterModal').classList.add('open');

      const res = await fetch('/api/cover-letter', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job_id: jobId, lang })
      });
      const data = await res.json();
      document.getElementById('letterModalSub').textContent =
        `${data.institution || ''} · ${data.job_title || ''}`;
      document.getElementById('letterTextArea').value = data.letter || '';
    }

    function closeLetterModal() {
      document.getElementById('letterModal').classList.remove('open');
    }

    function copyLetterText() {
      const ta = document.getElementById('letterTextArea');
      ta.select();
      document.execCommand('copy');
    }

    function downloadLetterFile() {
      const text = document.getElementById('letterTextArea').value || '';
      const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = currentLetterLang === 'en' ? 'Cover_Letter_EN.txt' : 'Anschreiben_DE.txt';
      a.click();
    }

    async function toggleHarvest() {
      await fetch('/api/harvest/toggle', { method: 'POST' });
      refreshDashboard();
      const timer = setInterval(async () => {
        const res = await fetch('/api/dashboard');
        const d = await res.json();
        refreshDashboard();
        if (!d.harvest.running) clearInterval(timer);
      }, 900);
    }

    async function runSystem2Discovery() {
      const btn = document.getElementById('sys2Btn');
      btn.textContent = '⏳ СИСТЕМА 2 СИНТЕЗИРУЕТ...';
      await fetch('/api/system2/discover', { method: 'POST' });
      refreshDashboard();
      const timer = setInterval(async () => {
        const res = await fetch('/api/dashboard');
        const d = await res.json();
        refreshDashboard();
        if (!d.harvest.system2_busy && !d.harvest.running) {
          btn.textContent = '🧠 НАЙТИ ИСТОЧНИКИ И АДАПТЕРЫ';
          clearInterval(timer);
        }
      }, 1100);
    }

    async function teachNewUrl() {
      const input = document.getElementById('teachUrlInput');
      const url = (input.value || '').trim();
      if (!url) return;
      document.getElementById('sys2Log').textContent = '⏳ Система 2 анализирует DOM и пишет адаптер...';
      await fetch('/api/system2/teach', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url })
      });
      input.value = '';
      refreshDashboard();
    }

    function startVoiceSearch() {
      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SpeechRecognition) {
        alert('Голосовой ввод поддерживается в Microsoft Edge и Google Chrome.');
        return;
      }
      const micBtn = document.getElementById('micBtn');
      const recognition = new SpeechRecognition();
      recognition.lang = 'ru-RU';
      recognition.interimResults = false;

      micBtn.classList.add('listening');
      micBtn.innerHTML = '🔴 ГОВОРИТЕ СЕЙЧАС...';

      recognition.onresult = async (event) => {
        const transcript = event.results[0][0].transcript;
        const parsedRes = await fetch('/api/voice/parse', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ transcript })
        });
        const parsed = await parsedRes.json();
        document.getElementById('searchInput').value = parsed.filter_query || transcript;
        document.getElementById('voiceChips').innerHTML =
          `<span class="chip">🎙 «${transcript}»</span>` +
          (parsed.detected_chips || []).map(c => `<span class="chip">${c}</span>`).join('');
        refreshDashboard();
      };

      recognition.onend = () => {
        micBtn.classList.remove('listening');
        micBtn.innerHTML = '🎤 СКАЗАТЬ, ЧТО ИСКАТЬ';
      };
      recognition.start();
    }

    async function uploadCVFile(file) {
      if (!file) return;
      const formData = new FormData();
      formData.append('file', file);
      await fetch('/api/cv/upload', { method: 'POST', body: formData });
      refreshDashboard();
    }

    const dz = document.getElementById('dropZone');
    dz.addEventListener('dragover', (e) => { e.preventDefault(); dz.classList.add('dragover'); });
    dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
    dz.addEventListener('drop', (e) => {
      e.preventDefault();
      dz.classList.remove('dragover');
      if (e.dataTransfer.files.length) uploadCVFile(e.dataTransfer.files[0]);
    });

    refreshDashboard();
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_HTML)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "admin_app:app",
        host="127.0.0.1",
        port=8090,
        reload=True,
        reload_excludes=["skills_registry/*", "data/*"],
    )
