# Academic Job Harvester (`PyCognition v1.3.0` · System 1 / System 2)

Neuro-symbolic academic & research job aggregator for Germany and the EU built on **PyCognition v1.3.0**, featuring:

- **System 2 (`core/cognitive_job_engine.py`)**: discovers university and research career portals (including HRK / Hochschulkompass / MWK / academics.de), synthesizes Python extractors, verifies them through `SkillCrystallizer` & `ASTSafetyAuditor` into `skills_registry/`, and repairs degraded skills automatically.
- **System 1 (`skills_registry/`)**: runs crystallized extractors deterministically with zero LLM overhead alongside Tier-1 adapters (`adapters/bundesagentur_adapter.py`, `adapters/euraxess_nds_adapter.py`).
- **Temporal & Entity Resolution (`AcademicTemporalBridge`)**: deduplicates cross-portal vacancies and tracks horizons `H1 / H2 / H3` via `TemporalEventGraph` and `NeuroSymbolicEntityResolver`.
- **Visual Control Center (`admin_app.py`)**: one-click harvest & System 2 discovery, voice-to-text search (`de-DE` / `ru-RU` / `en-US`), Drag-and-Drop CV matching (PDF/DOCX), live Chart.js infographics, and one-click *Anschreiben (DE)* / *Cover Letter (EN)* drafts.

## Quick Start (Windows `cmd.exe`)

```cmd
cd /d C:\Entwiklung\academic-job-harvester
call .venv\Scripts\activate.bat
python admin_app.py

Open http://127.0.0.1:8090 in your browser.
