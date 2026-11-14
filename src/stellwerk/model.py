"""Gemeinsame Datentypen der Verarbeitungskette."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Schweregrade. "pruefen" heißt: ein Mensch muss hinsehen, bevor zugestellt wird.
INFO, PRUEFEN, FEHLER = "info", "pruefen", "fehler"
_RANG = {INFO: 0, PRUEFEN: 1, FEHLER: 2}


@dataclass
class Finding:
    """Eine Beobachtung der Pipeline, so formuliert, dass man danach handeln kann."""

    code: str
    severity: str
    where: str
    message: str
    action: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class Line:
    """Eine Textzeile der Quelle mit Herkunft."""

    text: str
    origin: str = ""                 # "Seite 2", "OBX 4", "Spalte THERAPIE"
    conf: float | None = None        # OCR-Sicherheit 0..100; None = kein OCR
    uncertain: list[str] = field(default_factory=list)   # unsicher erkannte Wörter
    hint: str = ""                   # Überschrift, die die Quelle mitliefert (OBX-3, Spaltenname)


@dataclass
class Meta:
    patient_id: str = ""
    family: str = ""
    given: str = ""
    birth_date: str = ""             # FHIR date
    gender: str = ""                 # FHIR-Code
    gender_amtlich: str = ""         # D oder X, falls gender == other
    encounter_id: str = ""
    encounter_class: str = ""        # IMP | AMB | EMER | ...
    doc_id: str = ""
    doc_date: str = ""               # FHIR dateTime
    title: str = ""
    author: str = ""
    type_hint: str = ""              # Dokumenttyp laut Quelle (TXA-2, OBR-4)
    source_system: str = ""


@dataclass
class SourceDoc:
    kind: str                        # hl7 | pdf | sql | text
    name: str
    meta: Meta = field(default_factory=Meta)
    lines: list[Line] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    original: bytes | None = None
    original_type: str = ""
    stats: dict[str, Any] = field(default_factory=dict)


@dataclass
class Part:
    """Zusammenhängender Quellbereich, der einem Abschnitt zugeordnet wurde."""

    heading: str
    lines: list[Line]
    method: str                      # quelle | ueberschrift | unscharf | ki | position | rest
    score: float = 1.0
    note: str = ""


@dataclass
class Section:
    key: str
    title: str
    loinc: str = ""
    loinc_display: str = ""
    parts: list[Part] = field(default_factory=list)

    @property
    def lines(self) -> list[Line]:
        return [ln for p in self.parts for ln in p.lines]

    @property
    def methods(self) -> list[str]:
        return sorted({p.method for p in self.parts})

    @property
    def ocr_conf(self) -> float | None:
        vals = [(ln.conf, max(len(ln.text), 1)) for ln in self.lines if ln.conf is not None]
        if not vals:
            return None
        return sum(c * w for c, w in vals) / sum(w for _, w in vals)


def worst(findings: list[Finding]) -> str:
    """Gesamtstatus: ok, pruefen oder fehler."""
    rang = max((_RANG[f.severity] for f in findings), default=0)
    return {0: "ok", 1: "pruefen", 2: "fehler"}[rang]


def zahl(n: int, eins: str, viele: str) -> str:
    """„1 Zeile“, „3 Zeilen“."""
    return f"{n} {eins if n == 1 else viele}"
