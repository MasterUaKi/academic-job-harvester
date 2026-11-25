# File: core/escalation_config.py
# Version: 0.9.0 | Release Date: 2026-11-24
# EN: Configurable multi-step escalation policy (1..5+ attempts, progressive models
#     from simple/deterministic to deep reasoner) loaded from JSON/env.
# DE: Konfigurierbare Eskalationsstrategie (1..5+ Versuche, abgestufte Modelle).

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class EscalationAttemptConfig:
    attempt: int
    mode: str
    model: Optional[str]
    temperature: float
    dom_char_limit: int
    description: str = ""


class System2EscalationPolicy:
    DEFAULT_STEPS: List[Dict[str, Any]] = [
        {
            "attempt": 1,
            "mode": "deterministic_structural",
            "model": None,
            "temperature": 0.0,
            "dom_char_limit": 12000,
            "description": "Attempt 1: Fast deterministic DOM extractor",
        },
        {
            "attempt": 2,
            "mode": "llm",
            "model": "deepseek-chat",
            "temperature": 0.1,
            "dom_char_limit": 16000,
            "description": "Attempt 2: Fast LLM synthesis",
        },
        {
            "attempt": 3,
            "mode": "llm",
            "model": "deepseek-chat",
            "temperature": 0.2,
            "dom_char_limit": 24000,
            "description": "Attempt 3: Fast LLM + expanded DOM + diagnostics",
        },
        {
            "attempt": 4,
            "mode": "llm",
            "model": "deepseek-reasoner",
            "temperature": 0.1,
            "dom_char_limit": 28000,
            "description": "Attempt 4: Reasoner model synthesis",
        },
        {
            "attempt": 5,
            "mode": "llm",
            "model": "deepseek-reasoner",
            "temperature": 0.25,
            "dom_char_limit": 36000,
            "description": "Attempt 5: Reasoner model deep escalation",
        },
    ]

    def __init__(
        self,
        config_path: Path,
        default_fast_model: str = "deepseek-chat",
        default_reasoner_model: str = "deepseek-reasoner",
    ) -> None:
        self.config_path = config_path
        self.default_fast_model = default_fast_model
        self.default_reasoner_model = default_reasoner_model
        self.steps: List[EscalationAttemptConfig] = self._load_steps()

    def _load_steps(self) -> List[EscalationAttemptConfig]:
        raw_steps: List[Dict[str, Any]] = list(self.DEFAULT_STEPS)
        max_attempts_override: Optional[int] = None

        if self.config_path.is_file():
            try:
                cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(cfg, dict):
                    if isinstance(cfg.get("escalation_steps"), list) and cfg["escalation_steps"]:
                        raw_steps = cfg["escalation_steps"]
                    if cfg.get("max_attempts") is not None:
                        max_attempts_override = int(cfg["max_attempts"])
            except Exception:
                pass

        env_max = os.getenv("SYSTEM2_MAX_ATTEMPTS", "").strip()
        if env_max.isdigit():
            max_attempts_override = max(1, int(env_max))

        resolved: List[EscalationAttemptConfig] = []
        for idx, entry in enumerate(raw_steps, start=1):
            if not isinstance(entry, dict):
                continue
            mode = str(entry.get("mode") or "llm").strip()
            model_raw = entry.get("model")
            model_str = str(model_raw).strip() if model_raw else None
            if mode == "llm" and not model_str:
                model_str = self.default_reasoner_model if idx >= 4 else self.default_fast_model
            resolved.append(
                EscalationAttemptConfig(
                    attempt=int(entry.get("attempt") or idx),
                    mode=mode,
                    model=model_str,
                    temperature=float(entry.get("temperature", 0.1)),
                    dom_char_limit=int(entry.get("dom_char_limit", 20000)),
                    description=str(entry.get("description") or f"Attempt {idx}"),
                )
            )
        if max_attempts_override is not None:
            resolved = resolved[:max_attempts_override]
        return resolved
