# File: admin_app.py
# Version: 1.0.0 | Release Date: 2026-11-25
# EN: Text-only Academic Job Harvester v1.0.0 command center with System 1 / System 2
#     integration, interactive facet filters/sorting, CV matching, Anschreiben/Cover
#     Letter generator, and CSV/JSON export (voice input completely removed).
# DE: Reines Text-Dashboard `admin_app.py` v1.0.0 für Academic Job Harvester ohne
#     Spracheingabe, mit System 1/2, Filter-Chips, Anschreiben und CSV/JSON-Export.

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

_LOCAL_PYCOGNITION_SRC = Path(r"C:\Entwiklung\pycognition\src")
if _LOCAL_PYCOGNITION_SRC.exists() and str(_LOCAL_PYCOGNITION_SRC) not in sys.path:
    sys.path.insert(0, str(_LOCAL_PYCOGNITION_SRC))

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
from core.cover_letter_generator import build_letter_draft, polish_letter_via_deepseek
from core.cv_matcher import (
    build_cv_profile,
    extract_text_from_upload,
    load_cv_profile,
    score_and_filter_jobs,
)
from core.job_exporter import render_jobs_csv_bytes, render_jobs_json

try:
    from adapters.bundesagentur_adapter import BundesagenturJobAdapter
except ImportError:
    BundesagenturJobAdapter = None  # type: ignore

try:
    from adapters.euraxess_nds_adapter import EURAXESSAndNiedersachsenAdapter
except ImportError:
    EURAXESSAndNiedersachsenAdapter = None  # type: ignore


app = FastAPI(title="Academic Job Harvester — PyCognition Command Center", version="1.0.0")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CV_STATE_PATH = DATA_DIR / "active_cv_profile.json"
JOBS_CACHE_PATH = DATA_DIR / "harvested_jobs.json"

cognitive_engine = CognitiveJobSkillEngine(base_dir=BASE_DIR, data_dir=DATA_DIR)


def _classify_org_type(institution: str, source_portal: str = "") -> str:
    txt = f"{institution} {source_portal}".lower()
    if any(
        k in txt
        for k in (
            "max-planck", "max planck", "fraunhofer", "helmholtz", "leibniz",
            "dlr", "forschungszentrum", "institut", "awi", "tib", "forschungsinstitut",
        )
    ):
        return "НИИ / Forschungsinstitut"
    if any(k in txt for k in ("hochschule", "haw", "fh ", "fachhochschule")):
        return "Hochschule / HAW"
    if any(k in txt for k in ("universität", "universitaet", "uni ", "tu ", "rwth", "lmu", "kit")):
        return "Universität"
    if "euraxess" in txt:
        return "EURAXESS / EU"
    if "bundesagentur" in txt or "arbeitsagentur" in txt:
        return "Bundesagentur"
    return "Академическая организация"


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
                item["org_type"] = _classify_org_type(
                    str(item.get("institution") or ""),
                    str(item.get("source_portal") or ""),
                )
                valid.append(item)
        return valid
    except Exception:
        return []


def _save_jobs(jobs: List[Dict[str, Any]]) -> None:
    JOBS_CACHE_PATH.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")


def _filtered_jobs(
    q: str = "",
    org_type: str = "",
    institution: str = "",
    pay_grade: str = "",
    workload: str = "",
    contract: str = "",
    horizon: str = "",
    sort_by: str = "match",
) -> List[Dict[str, Any]]:
    cv = load_cv_profile(CV_STATE_PATH)
    return score_and_filter_jobs(
        _load_jobs(),
        cv.get("skills", []),
        query_filter=q,
        org_type_filter=org_type,
        institution_filter=institution,
        pay_grade_filter=pay_grade,
        workload_filter=workload,
        contract_filter=contract,
        horizon_filter=horizon,
        sort_by=sort_by,
    )


HARVEST_STATE: Dict[str, Any] = {
    "running": False,
    "system2_busy": False,
    "progress": 100,
    "active_source": "Готов · Нажмите «▶ ЗАПУСТИТЬ СБОР (СИСТЕМА 1)» или «🧠 ОБУЧИТЬ СИСТЕМУ 2»",
    "last_run": "2026-11-25 10:38 CET",
    "raw_collected": 0,
    "unique_canonical": 0,
    "last_api_hits": 0,
    "last_errors": [],
}


class TeachSourceRequest(BaseModel):
    url: str
    name: str = ""


class CoverLetterRequest(BaseModel):
    job_id: str
    lang: str = "de"


async def _execute_system1_cycle() -> None:
    HARVEST_STATE["running"] = True
    HARVEST_STATE["progress"] = 20
    HARVEST_STATE["active_source"] = "Система 1: запуск кристаллизованных скиллов (skills_registry/)..."
    HARVEST_STATE["last_errors"] = []

    collected: List[Dict[str, Any]] = await cognitive_engine.run_system1_harvest()
    HARVEST_STATE["progress"] = 55
    HARVEST_STATE["active_source"] = "Tier-1 Адаптеры: Bundesagentur API & EURAXESS..."

    if BundesagenturJobAdapter is not None:
        try:
            b_batch = await BundesagenturJobAdapter(size=25, require_academic_match=True).fetch_changes()
            HARVEST_STATE["last_api_hits"] = int(b_batch.metadata.get("api_raw_hits", 0))
            collected.extend(b_batch.records)
        except Exception as exc:
            HARVEST_STATE["last_errors"].append(str(exc))

    if EURAXESSAndNiedersachsenAdapter is not None:
        try:
            n_batch = await EURAXESSAndNiedersachsenAdapter().fetch_changes()
            collected.extend(n_batch.records)
        except Exception as exc:
            HARVEST_STATE["last_errors"].append(str(exc))

    dedup_by_url: Dict[str, Dict[str, Any]] = {}
    for rec in collected:
        u = str(rec.get("url") or "").strip()
        if u.startswith(("https://", "http://")):
            dedup_by_url[u] = rec

    HARVEST_STATE["progress"] = 85
    HARVEST_STATE["active_source"] = "PyCognition TemporalEventGraph & EntityResolver..."
    resolved_jobs = await temporal_bridge.ingest_jobs(
        list(dedup_by_url.values()),
        source_name="PyCognition System 1 + Tier-1 Adapters",
    )
    _save_jobs(resolved_jobs)

    s1_cnt = cognitive_engine.stats["system1_skills_count"]
    s1_ms = cognitive_engine.stats["system1_avg_ms"]
    HARVEST_STATE["raw_collected"] = len(collected) + HARVEST_STATE["last_api_hits"]
    HARVEST_STATE["unique_canonical"] = len(resolved_jobs)
    HARVEST_STATE["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    HARVEST_STATE["progress"] = 100
    HARVEST_STATE["active_source"] = f"Готово · Вакансий: {len(resolved_jobs)} (Скиллов S1: {s1_cnt}, {s1_ms} мс)"
    HARVEST_STATE["running"] = False


async def _execute_system2_discovery_cycle() -> None:
    HARVEST_STATE["system2_busy"] = True
    HARVEST_STATE["active_source"] = "Система 2: поиск источников и синтез скиллов..."
    try:
        await cognitive_engine.discover_and_build_adapters(max_new_sources=6)
        await _execute_system1_cycle()
    finally:
        HARVEST_STATE["system2_busy"] = False


@app.get("/api/dashboard")
async def get_dashboard_state(
    q: str = "",
    org_type: str = "",
    institution: str = "",
    pay_grade: str = "",
    workload: str = "",
    contract: str = "",
    horizon: str = "",
    sort_by: str = "match",
) -> JSONResponse:
    cv = load_cv_profile(CV_STATE_PATH)
    all_jobs = _load_jobs()
    jobs = _filtered_jobs(q, org_type, institution, pay_grade, workload, contract, horizon, sort_by)

    pay_dist: Dict[str, int] = {}
    horizon_dist: Dict[str, int] = {"H1": 0, "H2": 0, "H3": 0}
    for j in jobs:
        pg = str(j.get("pay_grade") or "TV-L / Tarif")
        pay_dist[pg] = pay_dist.get(pg, 0) + 1
        h = str(j.get("horizon") or "H3")
        horizon_dist[h] = horizon_dist.get(h, 0) + 1

    return JSONResponse({
        "harvest": HARVEST_STATE,
        "cognition_stats": cognitive_engine.stats,
        "system1_skills": cognitive_engine.list_skills_status(),
        "facets": {
            "org_types": sorted({_classify_org_type(str(j.get("institution") or ""), str(j.get("source_portal") or "")) for j in all_jobs}),
            "institutions": sorted({str(j.get("institution") or "") for j in all_jobs if j.get("institution")}),
            "pay_grades": sorted({str(j.get("pay_grade") or "TV-L / Tarif") for j in all_jobs}),
            "workloads": sorted({str(j.get("workload") or "Siehe Ausschreibung") for j in all_jobs}),
            "contracts": sorted({str(j.get("contract") or "Ausschreibung") for j in all_jobs}),
        },
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
            "matched": sum(1 for j in jobs if j["match_score"] >= 60),
        },
        "pay_distribution": pay_dist,
        "horizon_distribution": horizon_dist,
        "jobs": jobs[:50],
    })


@app.get("/api/export/json")
async def export_jobs_json(
    q: str = "", org_type: str = "", institution: str = "", pay_grade: str = "",
    workload: str = "", contract: str = "", horizon: str = "", sort_by: str = "match",
) -> Response:
    jobs = _filtered_jobs(q, org_type, institution, pay_grade, workload, contract, horizon, sort_by)
    return Response(
        content=render_jobs_json(jobs),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="academic_jobs.json"'},
    )


@app.get("/api/export/csv")
async def export_jobs_csv(
    q: str = "", org_type: str = "", institution: str = "", pay_grade: str = "",
    workload: str = "", contract: str = "", horizon: str = "", sort_by: str = "match",
) -> Response:
    jobs = _filtered_jobs(q, org_type, institution, pay_grade, workload, contract, horizon, sort_by)
    return Response(
        content=render_jobs_csv_bytes(jobs),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="academic_jobs.csv"'},
    )


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
    cv = load_cv_profile(CV_STATE_PATH)
    job = next((item for item in _load_jobs() if str(item.get("id")) == req.job_id), None)
    if not job:
        return JSONResponse({"status": "error", "letter": "Вакансия не найдена."}, status_code=404)
    draft = build_letter_draft(job=job, cv=cv, lang=req.lang)
    polished = await polish_letter_via_deepseek(draft=draft, lang=req.lang)
    return JSONResponse({"status": "ok", "job_title": job.get("title"), "institution": job.get("institution"), "lang": req.lang, "letter": polished})


@app.post("/api/cv/upload")
async def upload_cv(file: UploadFile = File(...)) -> JSONResponse:
    content = await file.read()
    text = extract_text_from_upload(file.filename or "resume.txt", content)
    profile = build_cv_profile(file.filename or "resume.txt", text, CV_STATE_PATH)
    return JSONResponse({"status": "ok", "profile": profile})


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Academic Job Harvester · PyCognition v1.0</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
  <style>
    :root { --bg: #0b1120; --card: #131c31; --border: #243354; --text: #f1f5f9; --muted: #94a3b8; --green: #10b981; --blue: #3b82f6; --amber: #f59e0b; --purple: #8b5cf6; }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); padding: 20px; }
    .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 18px; padding-bottom: 14px; border-bottom: 1px solid var(--border); }
    .brand { display: flex; align-items: center; gap: 12px; }
    .brand-badge { background: linear-gradient(135deg, var(--blue), var(--purple)); padding: 8px 14px; border-radius: 12px; font-weight: 800; font-size: 17px; }
    .big-controls { display: grid; grid-template-columns: 1.1fr 1.2fr 1fr; gap: 14px; margin-bottom: 18px; }
    .control-tile { background: var(--card); border: 2px solid var(--border); border-radius: 18px; padding: 16px; display: flex; flex-direction: column; justify-content: space-between; }
    .btn-giant { width: 100%; padding: 15px 12px; border: none; border-radius: 14px; font-size: 15px; font-weight: 800; color: #fff; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 8px; }
    .btn-start { background: linear-gradient(135deg, #059669, #10b981); }
    .btn-stop  { background: linear-gradient(135deg, #dc2626, #ef4444); }
    .btn-sys2  { background: linear-gradient(135deg, #7c3aed, #8b5cf6); }
    .progress-bar { height: 8px; background: #1e293b; border-radius: 999px; overflow: hidden; margin-top: 10px; }
    .progress-fill { height: 100%; background: var(--green); width: 100%; transition: width 0.4s; }
    .drop-zone { border: 2px dashed #475569; border-radius: 14px; padding: 12px; text-align: center; cursor: pointer; background: rgba(30, 41, 59, 0.45); }
    .drop-zone.dragover { border-color: var(--green); background: rgba(16, 185, 129, 0.15); }
    .chip-row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
    .chip { font-size: 11px; padding: 3px 9px; border-radius: 999px; background: rgba(59, 130, 246, 0.2); border: 1px solid rgba(59, 130, 246, 0.45); color: #93c5fd; }
    .chip-s1 { font-size: 11px; padding: 4px 10px; border-radius: 999px; background: rgba(16, 185, 129, 0.16); border: 1px solid rgba(16, 185, 129, 0.45); color: #6ee7b7; }
    .facets-panel { background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 14px 18px; margin-bottom: 18px; display: flex; flex-direction: column; gap: 10px; }
    .facet-group { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
    .facet-label { font-size: 11px; color: var(--muted); font-weight: 700; min-width: 125px; text-transform: uppercase; }
    .facet-btn { background: #1e293b; color: #cbd5e1; border: 1px solid var(--border); padding: 5px 11px; border-radius: 999px; font-size: 12px; font-weight: 600; cursor: pointer; }
    .facet-btn.active { background: linear-gradient(135deg, #2563eb, #7c3aed); color: #fff; border-color: transparent; }
    .sort-select { background: #0f172a; color: var(--text); border: 1px solid var(--border); padding: 6px 10px; border-radius: 9px; font-size: 12px; font-weight: 600; }
    .charts-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 20px; }
    .chart-card { background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 16px; }
    .chart-card h3 { font-size: 13px; color: var(--muted); margin-bottom: 10px; text-transform: uppercase; }
    .chart-wrap { height: 180px; position: relative; }
    .skills-bar { background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 14px 18px; margin-bottom: 20px; }
    .jobs-section { background: var(--card); border: 1px solid var(--border); border-radius: 18px; padding: 18px; }
    .jobs-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; gap: 10px; flex-wrap: wrap; }
    .export-group { display: flex; gap: 8px; align-items: center; }
    .btn-export { background: #1e293b; color: #e2e8f0; border: 1px solid var(--border); padding: 7px 12px; border-radius: 9px; font-size: 12px; font-weight: 700; cursor: pointer; }
    .job-row { display: grid; grid-template-columns: 84px 1fr auto; gap: 16px; align-items: center; padding: 14px; border-bottom: 1px solid var(--border); }
    .job-row:last-child { border-bottom: none; }
    .job-actions { display: flex; flex-direction: column; gap: 8px; min-width: 155px; }
    .btn-open { background: #1e293b; color: #f8fafc; padding: 9px 12px; border-radius: 10px; text-decoration: none; font-size: 12px; font-weight: 700; border: 1px solid var(--border); text-align: center; }
    .btn-letter { background: rgba(59, 130, 246, 0.18); color: #93c5fd; padding: 8px 10px; border-radius: 10px; border: 1px solid rgba(59, 130, 246, 0.45); font-size: 11px; font-weight: 700; cursor: pointer; }
    .score-badge { width: 74px; height: 74px; border-radius: 16px; display: flex; flex-direction: column; align-items: center; justify-content: center; font-weight: 800; font-size: 20px; background: rgba(16, 185, 129, 0.16); border: 2px solid var(--green); color: #6ee7b7; }
    .score-badge.mid { background: rgba(245, 158, 11, 0.16); border-color: var(--amber); color: #fcd34d; }
    .score-badge span { font-size: 10px; font-weight: 600; color: var(--muted); }
    .meta-pills { display: flex; flex-wrap: wrap; gap: 8px; margin: 6px 0; font-size: 12px; }
    .pill { background: #1e293b; padding: 3px 10px; border-radius: 8px; color: #cbd5e1; }
    .pill-src { background: rgba(139, 92, 246, 0.2); border: 1px solid rgba(139, 92, 246, 0.45); color: #ddd6fe; padding: 3px 10px; border-radius: 8px; }
    .tag-ok { background: rgba(16, 185, 129, 0.2); color: #6ee7b7; padding: 2px 8px; border-radius: 6px; font-size: 11px; }
    .tag-miss { background: rgba(245, 158, 11, 0.18); color: #fcd34d; padding: 2px 8px; border-radius: 6px; font-size: 11px; }
    .text-input { width: 100%; margin-top: 8px; padding: 9px 12px; border-radius: 10px; border: 1px solid var(--border); background: #0f172a; color: var(--text); font-size: 13px; }
    .teach-row { display: flex; gap: 6px; margin-top: 8px; }
    .btn-mini { background: #334155; color: #fff; border: none; border-radius: 9px; padding: 0 12px; font-weight: 700; cursor: pointer; font-size: 12px; }
    .empty-state { padding: 26px; text-align: center; color: var(--muted); font-size: 14px; }
    .modal-backdrop { display: none; position: fixed; inset: 0; background: rgba(2, 6, 23, 0.78); align-items: center; justify-content: center; z-index: 999; padding: 20px; }
    .modal-backdrop.open { display: flex; }
    .modal-box { background: var(--card); border: 2px solid var(--border); border-radius: 18px; width: 100%; max-width: 720px; padding: 20px; }
    .letter-area { width: 100%; min-height: 290px; margin: 12px 0; padding: 14px; border-radius: 12px; border: 1px solid var(--border); background: #0b1120; color: var(--text); font-family: inherit; font-size: 13px; line-height: 1.5; }
  </style>
</head>
<body>
  <div class="header">
    <div class="brand">
      <div class="brand-badge">PyCognition v1.0</div>
      <div>
        <h1 style="font-size:19px;">Academic Job Harvester · Пульт управления (текстовый режим)</h1>
        <div id="temporalMeta" style="font-size:12px; color:var(--muted);">Система 1 + Система 2 · Поиск, фильтрация и отклики</div>
      </div>
    </div>
    <div id="cognitionBadge" style="font-size:12px; color:#c4b5fd; font-weight:600;">⚡ S1 Скиллов: 0 · Синтезов S2: 0</div>
  </div>

  <div class="big-controls">
    <div class="control-tile">
      <div>
        <div style="font-size:11px; color:var(--muted); margin-bottom:6px;">1 · СИСТЕМА 1 (СБОР ВАКАНСИЙ)</div>
        <button id="harvestBtn" class="btn-giant btn-start" onclick="toggleHarvest()">▶ ЗАПУСТИТЬ СБОР</button>
      </div>
      <div>
        <div class="progress-bar"><div id="progressFill" class="progress-fill"></div></div>
        <div id="sourceLabel" style="font-size:11px; color:var(--muted); margin-top:6px;">Ожидание запуска</div>
      </div>
    </div>
    <div class="control-tile">
      <div>
        <div style="font-size:11px; color:var(--muted); margin-bottom:6px;">2 · СИСТЕМА 2 (ПОИСК ВУЗОВ И СИНТЕЗ)</div>
        <button id="sys2Btn" class="btn-giant btn-sys2" onclick="runSystem2Discovery()">🧠 НАЙТИ ИСТОЧНИКИ И АДАПТЕРЫ</button>
      </div>
      <div>
        <div class="teach-row">
          <input id="teachUrlInput" class="text-input" style="margin-top:0;" placeholder="Вставьте URL вуза (напр. uni-muenster.de/...)" />
          <button class="btn-mini" onclick="teachNewUrl()">+ Обучить</button>
        </div>
        <div id="sys2Log" style="font-size:11px; color:#c4b5fd; margin-top:6px;">Эскалация до 5 шагов</div>
      </div>
    </div>
    <div class="control-tile">
      <div style="font-size:11px; color:var(--muted); margin-bottom:6px;">3 · ПЕРЕТАЩИТЕ РЕЗЮМЕ (PDF/DOCX)</div>
      <div id="dropZone" class="drop-zone" onclick="document.getElementById('cvFile').click()">
        <div style="font-size:20px;">📄⬇️</div>
        <div id="cvTitle" style="font-weight:700; font-size:13px; margin-top:3px;">Бросьте резюме сюда</div>
        <div id="cvSub" style="font-size:10px; color:var(--muted);">Рейтинг совпадений</div>
        <input id="cvFile" type="file" accept=".pdf,.docx,.txt" style="display:none" onchange="uploadCVFile(this.files[0])" />
      </div>
      <div id="cvSkills" class="chip-row"></div>
    </div>
  </div>

  <div class="facets-panel">
    <div class="facet-group">
      <span class="facet-label">🔎 ТЕКСТОВЫЙ ПОИСК:</span>
      <input id="searchInput" class="text-input" style="margin-top:0; max-width:420px;" placeholder="Ключевые слова: E13, Vechta, Oldenburg, CRIS, PDF..." oninput="refreshDashboard()" />
    </div>
    <div class="facet-group"><span class="facet-label">🏢 ТИП ВУЗА / НИИ:</span><div id="orgTypeChips" style="display:flex; flex-wrap:wrap; gap:6px;"></div></div>
    <div class="facet-group"><span class="facet-label">🏛 ОРГАНИЗАЦИЯ:</span><div id="institutionChips" style="display:flex; flex-wrap:wrap; gap:6px;"></div></div>
    <div class="facet-group"><span class="facet-label">💶 ТАРИФ:</span><div id="payGradeChips" style="display:flex; flex-wrap:wrap; gap:6px;"></div></div>
    <div class="facet-group"><span class="facet-label">⏱ ЗАНЯТОСТЬ:</span><div id="workloadChips" style="display:flex; flex-wrap:wrap; gap:6px;"></div></div>
    <div class="facet-group"><span class="facet-label">📄 ФОРМАТ / СРОК:</span><div id="contractChips" style="display:flex; flex-wrap:wrap; gap:6px;"></div></div>
    <div class="facet-group" style="justify-content:space-between;">
      <div style="display:flex; flex-wrap:wrap; align-items:center; gap:6px;">
        <span class="facet-label">⏳ ГОРИЗОНТ:</span>
        <button class="facet-btn active" data-hz="" onclick="setHorizon('')">Все</button>
        <button class="facet-btn" data-hz="H1" onclick="setHorizon('H1')">H1 (≤2 дн)</button>
        <button class="facet-btn" data-hz="H2" onclick="setHorizon('H2')">H2 (3–14 дн)</button>
        <button class="facet-btn" data-hz="H3" onclick="setHorizon('H3')">H3 (14+ дн)</button>
      </div>
      <div style="display:flex; align-items:center; gap:8px;">
        <span style="font-size:11px; color:var(--muted); font-weight:700;">СОРТИРОВКА:</span>
        <select id="sortSelect" class="sort-select" onchange="setSortBy(this.value)">
          <option value="match">По совпадению с CV (%)</option>
          <option value="org_type">По типу организации (ВУЗ / НИИ)</option>
          <option value="institution">По названию организации (A–Z)</option>
          <option value="pay_grade">По тарифному разряду</option>
          <option value="workload">По занятости / ставке</option>
          <option value="title">По названию вакансии (A–Z)</option>
        </select>
        <button class="btn-export" onclick="resetAllFilters()">↺ Сбросить</button>
      </div>
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
    <div class="chart-card"><h3>1. Воронка дедупликации PyCognition</h3><div class="chart-wrap"><canvas id="funnelChart"></canvas></div></div>
    <div class="chart-card"><h3>2. Тарифные группы (TV-L / TVöD / W)</h3><div class="chart-wrap"><canvas id="payChart"></canvas></div></div>
    <div class="chart-card"><h3>3. Радар дедлайнов (H1 / H2 / H3)</h3><div class="chart-wrap"><canvas id="horizonChart"></canvas></div></div>
  </div>

  <div class="jobs-section">
    <div class="jobs-header">
      <h2 style="font-size:16px;">🎯 Реальные вакансии с прямыми ссылками</h2>
      <div class="export-group">
        <span id="jobsCount" style="font-size:13px; color:var(--muted);"></span>
        <button class="btn-export" onclick="exportJobs('csv')">⬇ CSV</button>
        <button class="btn-export" onclick="exportJobs('json')">⬇ JSON</button>
      </div>
    </div>
    <div id="jobsList"></div>
  </div>

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
    let funnelChart, payChart, horizonChart, currentLetterLang = 'de', lastChartSignature = '';
    let activeOrgType = '', activeInstitution = '', activePayGrade = '', activeWorkload = '', activeContract = '', activeHorizon = '', activeSortBy = 'match';

    function buildQueryParams() {
      return new URLSearchParams({
        q: document.getElementById('searchInput').value || '',
        org_type: activeOrgType, institution: activeInstitution, pay_grade: activePayGrade,
        workload: activeWorkload, contract: activeContract, horizon: activeHorizon, sort_by: activeSortBy,
      }).toString();
    }
    function exportJobs(fmt) { window.open(`/api/export/${fmt}?${buildQueryParams()}`, '_blank'); }
    function toggleFacet(kind, val) {
      if (kind === 'org_type') activeOrgType = (activeOrgType === val) ? '' : val;
      if (kind === 'institution') activeInstitution = (activeInstitution === val) ? '' : val;
      if (kind === 'pay_grade') activePayGrade = (activePayGrade === val) ? '' : val;
      if (kind === 'workload') activeWorkload = (activeWorkload === val) ? '' : val;
      if (kind === 'contract') activeContract = (activeContract === val) ? '' : val;
      refreshDashboard();
    }
    function setHorizon(hz) {
      activeHorizon = hz;
      document.querySelectorAll('[data-hz]').forEach(b => b.classList.toggle('active', b.getAttribute('data-hz') === hz));
      refreshDashboard();
    }
    function setSortBy(val) { activeSortBy = val; refreshDashboard(); }
    function resetAllFilters() {
      activeOrgType = activeInstitution = activePayGrade = activeWorkload = activeContract = activeHorizon = '';
      activeSortBy = 'match';
      document.getElementById('searchInput').value = '';
      document.getElementById('sortSelect').value = 'match';
      setHorizon('');
    }
    function chipButtons(items, activeVal, kind, limit) {
      return (items || []).slice(0, limit).map(v => {
        const act = activeVal.toLowerCase() === String(v).toLowerCase();
        const safe = String(v).replace(/'/g, "\\'");
        return `<button class="facet-btn ${act ? 'active' : ''}" onclick="toggleFacet('${kind}', '${safe}')">${v}</button>`;
      }).join('');
    }
    function renderFacetChips(data) {
      const f = data.facets || {};
      document.getElementById('orgTypeChips').innerHTML = chipButtons(f.org_types, activeOrgType, 'org_type', 10) || '<span style="font-size:12px;color:var(--muted);">Появятся после сбора</span>';
      document.getElementById('institutionChips').innerHTML = chipButtons(f.institutions, activeInstitution, 'institution', 12);
      document.getElementById('payGradeChips').innerHTML = chipButtons(f.pay_grades, activePayGrade, 'pay_grade', 10);
      document.getElementById('workloadChips').innerHTML = chipButtons(f.workloads, activeWorkload, 'workload', 8);
      document.getElementById('contractChips').innerHTML = chipButtons(f.contracts, activeContract, 'contract', 8);
    }
    async function refreshDashboard() {
      const data = await (await fetch('/api/dashboard?' + buildQueryParams())).json();
      const btn = document.getElementById('harvestBtn');
      btn.className = data.harvest.running ? 'btn-giant btn-stop' : 'btn-giant btn-start';
      btn.innerHTML = data.harvest.running ? `⏹ СТОП (${data.harvest.progress}%)` : '▶ ЗАПУСТИТЬ СБОР';
      document.getElementById('progressFill').style.width = data.harvest.progress + '%';
      document.getElementById('sourceLabel').textContent = data.harvest.active_source;

      if (data.cognition_stats) {
        const cs = data.cognition_stats;
        document.getElementById('cognitionBadge').textContent =
          `⚡ S1: ${cs.system1_skills_count} (${cs.system1_avg_ms} мс) · S2: ${cs.system2_synthesized} · Шагов: ${cs.system2_max_attempts || 5}`;
        document.getElementById('sys2Log').textContent = cs.last_system2_log || '';
      }
      if (data.temporal_stats) {
        document.getElementById('temporalMeta').textContent =
          `Уникальных вакансий: ${data.temporal_stats.canonical_entities} · Склеено дублей: ${data.temporal_stats.aliases_merged}`;
      }
      renderFacetChips(data);

      const skills = data.system1_skills || [];
      document.getElementById('skillsSummary').textContent = `Активно ${skills.filter(s => s.crystallized).length} из ${skills.length}`;
      document.getElementById('skillsChips').innerHTML = skills.map(s =>
        `<span class="${s.crystallized ? 'chip-s1' : 'chip'}">${s.crystallized ? '✓' : '⏳'} ${s.institution || s.name}</span>`
      ).join('');

      document.getElementById('cvTitle').textContent = '✅ ' + data.cv_profile.filename;
      document.getElementById('cvSub').textContent = data.cv_profile.preview;
      document.getElementById('cvSkills').innerHTML = (data.cv_profile.skills || []).slice(0, 6).map(s => `<span class="chip">${s}</span>`).join('');

      const sig = JSON.stringify({ funnel: data.funnel, pay: data.pay_distribution, horizon: data.horizon_distribution });
      if (sig !== lastChartSignature) { lastChartSignature = sig; renderCharts(data); }
      renderJobs(data.jobs);
    }
    function renderCharts(data) {
      const opts = { responsive: true, maintainAspectRatio: false, animation: false, plugins: { legend: { labels: { color: '#cbd5e1', font: { size: 11 } } } } };
      if (funnelChart) funnelChart.destroy();
      funnelChart = new Chart(document.getElementById('funnelChart'), {
        type: 'bar',
        data: { labels: ['Проверено', 'Прямые ссылки', 'Подходят под CV'], datasets: [{ data: [data.funnel.raw, data.funnel.unique, data.funnel.matched], backgroundColor: ['#3b82f6', '#8b5cf6', '#10b981'], borderRadius: 8 }] },
        options: { ...opts, plugins: { legend: { display: false } }, scales: { x: { ticks: { color: '#94a3b8' } }, y: { ticks: { color: '#94a3b8' } } } }
      });
      if (payChart) payChart.destroy();
      payChart = new Chart(document.getElementById('payChart'), {
        type: 'doughnut',
        data: { labels: Object.keys(data.pay_distribution).length ? Object.keys(data.pay_distribution) : ['Ожидание'], datasets: [{ data: Object.values(data.pay_distribution).length ? Object.values(data.pay_distribution) : [1], backgroundColor: ['#10b981', '#3b82f6', '#8b5cf6', '#f59e0b', '#ec4899'], borderWidth: 0 }] },
        options: opts
      });
      if (horizonChart) horizonChart.destroy();
      horizonChart = new Chart(document.getElementById('horizonChart'), {
        type: 'bar',
        data: { labels: ['H1 (≤2 дн)', 'H2 (3–14 дн)', 'H3 (14+ дн)'], datasets: [{ data: [data.horizon_distribution.H1, data.horizon_distribution.H2, data.horizon_distribution.H3], backgroundColor: ['#ef4444', '#f59e0b', '#10b981'], borderRadius: 8 }] },
        options: { ...opts, indexAxis: 'y', plugins: { legend: { display: false } }, scales: { x: { ticks: { color: '#94a3b8' } }, y: { ticks: { color: '#94a3b8' } } } }
      });
    }
    function renderJobs(jobs) {
      document.getElementById('jobsCount').textContent = `Показано вакансий: ${jobs.length}`;
      const el = document.getElementById('jobsList');
      if (!jobs.length) { el.innerHTML = `<div class="empty-state">Вакансий по текущим фильтрам не найдено. Нажмите «↺ Сбросить» или запустите сбор.</div>`; return; }
      el.innerHTML = jobs.map(j => `
        <div class="job-row">
          <div class="score-badge ${j.match_score < 60 ? 'mid' : ''}">${j.match_score}%<span>СОВПАДЕНИЕ</span></div>
          <div>
            <div style="font-weight:700; font-size:15px;">${j.title}</div>
            <div style="font-size:13px; color:#93c5fd; margin-top:2px;">🏛 ${j.institution} · <span style="color:#c4b5fd;">${j.org_type || ''}</span> · 📍 ${j.location}</div>
            <div class="meta-pills">
              <span class="pill">💶 ${j.pay_grade}</span><span class="pill">⏱ ${j.workload}</span>
              <span class="pill">📄 ${j.contract}</span><span class="pill">📅 ${j.deadline}</span>
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
      document.getElementById('letterModalTitle').textContent = lang === 'en' ? '✉ Cover Letter Draft (EN)' : '✉ Anschreiben-Entwurf (DE)';
      document.getElementById('letterModalSub').textContent = 'Формируется...';
      document.getElementById('letterTextArea').value = '';
      document.getElementById('letterModal').classList.add('open');
      const data = await (await fetch('/api/cover-letter', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ job_id: jobId, lang }) })).json();
      document.getElementById('letterModalSub').textContent = `${data.institution || ''} · ${data.job_title || ''}`;
      document.getElementById('letterTextArea').value = data.letter || '';
    }
    function closeLetterModal() { document.getElementById('letterModal').classList.remove('open'); }
    function copyLetterText() { const ta = document.getElementById('letterTextArea'); ta.select(); document.execCommand('copy'); }
    function downloadLetterFile() {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(new Blob([document.getElementById('letterTextArea').value || ''], { type: 'text/plain;charset=utf-8' }));
      a.download = currentLetterLang === 'en' ? 'Cover_Letter_EN.txt' : 'Anschreiben_DE.txt';
      a.click();
    }
    async function toggleHarvest() {
      await fetch('/api/harvest/toggle', { method: 'POST' });
      refreshDashboard();
      const timer = setInterval(async () => { const d = await (await fetch('/api/dashboard')).json(); refreshDashboard(); if (!d.harvest.running) clearInterval(timer); }, 900);
    }
    async function runSystem2Discovery() {
      const btn = document.getElementById('sys2Btn');
      btn.textContent = '⏳ СИСТЕМА 2 СИНТЕЗИРУЕТ...';
      await fetch('/api/system2/discover', { method: 'POST' });
      refreshDashboard();
      const timer = setInterval(async () => { const d = await (await fetch('/api/dashboard')).json(); refreshDashboard(); if (!d.harvest.system2_busy && !d.harvest.running) { btn.textContent = '🧠 НАЙТИ ИСТОЧНИКИ И АДАПТЕРЫ'; clearInterval(timer); } }, 1100);
    }
    async function teachNewUrl() {
      const inp = document.getElementById('teachUrlInput');
      const url = (inp.value || '').trim();
      if (!url) return;
      document.getElementById('sys2Log').textContent = '⏳ Система 2 анализирует DOM...';
      await fetch('/api/system2/teach', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) });
      inp.value = '';
      refreshDashboard();
    }
    async function uploadCVFile(file) {
      if (!file) return;
      const fd = new FormData();
      fd.append('file', file);
      await fetch('/api/cv/upload', { method: 'POST', body: fd });
      refreshDashboard();
    }
    const dz = document.getElementById('dropZone');
    dz.addEventListener('dragover', (e) => { e.preventDefault(); dz.classList.add('dragover'); });
    dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
    dz.addEventListener('drop', (e) => { e.preventDefault(); dz.classList.remove('dragover'); if (e.dataTransfer.files.length) uploadCVFile(e.dataTransfer.files[0]); });
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
    uvicorn.run("admin_app:app", host="127.0.0.1", port=8090, reload=True, reload_excludes=["skills_registry/*", "data/*"])
