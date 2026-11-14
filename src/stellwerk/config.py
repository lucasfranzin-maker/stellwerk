"""Konfiguration: Vorgaben, Datei stellwerk.yaml, Umgebungsvariablen."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "vorlage": "entlassbrief",
    # Dokumenttyp der Quelle (TXA-2, OBR-4) -> Vorlage
    "vorlagen_zuordnung": {},
    "ziel": "isik-dokument",            # oder "transaktion"
    "zeitzone": "Europe/Berlin",
    "systeme": {"patient": None, "fall": None, "dokument": None},
    "standard": {"fallart": None, "autor": None},
    "ocr": {"modus": "auto", "sprache": "deu", "dpi": 300,
            "seite_warnen_unter": 75, "zahl_warnen_unter": 80, "wort_unsicher_unter": 60},
    "llm": {"url": "http://localhost:11434", "modell": "", "api": "ollama", "timeout": 120,
            "nur_lokal": True, "erlaubte_hosts": []},
    "zustellung": {"ziel": None, "bei_pruefen": "zurueckhalten"},
    "ausgabe": "out",
}

PLATZHALTER = {
    "patient": "https://example.org/fhir/sid/patientennummer",
    "fall": "https://example.org/fhir/sid/aufnahmenummer",
    "dokument": "https://example.org/fhir/sid/dokumentnummer",
}


def _merge(base: dict, extra: dict) -> dict:
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load(path: str | os.PathLike | None = None) -> dict[str, Any]:
    """Vorgaben, darüber stellwerk.yaml (falls vorhanden), darüber Umgebungsvariablen."""
    cfg = copy.deepcopy(DEFAULTS)
    p = Path(path) if path else Path("stellwerk.yaml")
    if path and not p.exists():
        raise FileNotFoundError(f"Konfigurationsdatei nicht gefunden: {p}")
    if p.exists():
        _merge(cfg, yaml.safe_load(p.read_text(encoding="utf-8")) or {})
        cfg["_datei"] = str(p)
    env = os.environ
    if env.get("STELLWERK_LLM_URL"):
        cfg["llm"]["url"] = env["STELLWERK_LLM_URL"]
    if env.get("STELLWERK_LLM_MODELL"):
        cfg["llm"]["modell"] = env["STELLWERK_LLM_MODELL"]
    if env.get("STELLWERK_LLM_API"):
        cfg["llm"]["api"] = env["STELLWERK_LLM_API"]
    return cfg
