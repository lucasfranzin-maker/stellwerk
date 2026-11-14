"""Die Verarbeitungskette: Eingang lesen, Abschnitte zuordnen, Bundle bauen, ablegen, zustellen."""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import hl7v2
from .fhir import build_bundle
from .llm import LLMError, LocalLLM
from .model import FEHLER, INFO, PRUEFEN, Finding, Line, Section, SourceDoc, worst
from .sections import Template, load_template, sectionize
from .textrepair import clean_text, decode_bytes, strip_markup


@dataclass
class Result:
    doc: SourceDoc
    tpl: Template
    sections: list[Section] = field(default_factory=list)
    bundle: dict | None = None
    findings: list[Finding] = field(default_factory=list)
    llm: str = ""                    # Bezeichnung des Modells oder Grund, warum keins lief
    delivery: str = ""               # was mit dem Bundle geschah
    delivered: bool = False
    files: dict[str, str] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return worst(self.findings)


def make_llm(cfg: dict) -> tuple[LocalLLM | None, str]:
    c = cfg.get("llm") or {}
    if not c.get("modell"):
        return None, "kein lokales Modell eingestellt"
    try:
        return LocalLLM(c), ""
    except LLMError as exc:
        return None, str(exc)


def sniff(data: bytes) -> str:
    head = data.lstrip(b"\x0b\xef\xbb\xbf\r\n\t ")[:8]
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith((b"MSH", b"FHS", b"BHS")):
        return "hl7"
    return "text"


def ingest(data: bytes, name: str, cfg: dict, meta: dict | None = None) -> list[SourceDoc]:
    """Datei lesen. Eine HL7-Datei kann mehrere Nachrichten und damit mehrere Dokumente enthalten."""
    kind = sniff(data)
    if kind == "hl7":
        parts = hl7v2.split_messages(data) or [data]
        docs = []
        for i, raw in enumerate(parts, 1):
            label = name if len(parts) == 1 else f"{name} · Nachricht {i}"
            d = hl7v2.document_from_message(hl7v2.parse(raw), label, cfg)
            if d.original is None:
                d.original, d.original_type = raw, "x-application/hl7-v2+er7"
            d.stats["eingang"] = raw
            docs.append(d)
    elif kind == "pdf":
        from .pdftext import pdf_to_lines
        doc = SourceDoc("pdf", name, original=data, original_type="application/pdf")
        doc.lines, doc.findings, doc.stats = pdf_to_lines(data, cfg)
        docs = [doc]
    else:
        doc = SourceDoc("text", name, original=data, original_type="text/plain")
        text, f1 = decode_bytes(data, None, name)
        text, f2 = strip_markup(text, name)
        text, f3 = clean_text(text, name)
        doc.findings = f1 + f2 + f3
        doc.lines = [Line(x.rstrip(), "Text") for x in text.split("\n")]
        docs = [doc]
    for d in docs:
        for k, v in (meta or {}).items():
            if v:
                setattr(d.meta, k, v)
    return docs


_GEB = re.compile(r"(?i)\b(?:geb\.?|geboren(?: am)?|geburtsdatum|\*)\s*:?\s*(?:am\s+)?(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})")


def crosscheck_patient(doc: SourceDoc) -> list[Finding]:
    """Passt das Geburtsdatum im Dokumenttext zum Patienten aus den Kopfdaten?"""
    if not doc.meta.birth_date:
        return []
    found = {f"{y}-{int(mo):02d}-{int(d):02d}" for ln in doc.lines for d, mo, y in _GEB.findall(ln.text)}
    if not found or doc.meta.birth_date in found:
        return []
    anders = ", ".join(f"{x[8:]}.{x[5:7]}.{x[:4]}" for x in sorted(found))
    soll = doc.meta.birth_date
    return [Finding(
        "PAT_ABWEICHUNG", PRUEFEN, "Patient",
        f"Im Dokumenttext steht das Geburtsdatum {anders}, die Kopfdaten nennen {soll[8:]}.{soll[5:7]}.{soll[:4]}. "
        "Möglicherweise hängt das Dokument am falschen Patienten.",
        "Patientenzuordnung prüfen, bevor das Dokument ins CDR geht. Bei gescannten Dokumenten kann es auch ein Lesefehler sein.")]


def process(doc: SourceDoc, cfg: dict, vorlage: str | None = None, llm: LocalLLM | None = None,
            llm_note: str = "", now=None, tpl: Template | None = None) -> Result:
    name = vorlage or (cfg.get("vorlagen_zuordnung") or {}).get(doc.meta.type_hint) or cfg.get("vorlage", "entlassbrief")
    tpl = tpl or load_template(name)
    res = Result(doc, tpl, findings=list(doc.findings), llm=llm.label if llm else llm_note)
    if any(f.severity == FEHLER for f in res.findings) and not any(ln.text.strip() for ln in doc.lines):
        return res
    res.sections, f = sectionize(doc, tpl, llm)
    res.findings += f + crosscheck_patient(doc)
    res.bundle, f = build_bundle(doc, res.sections, tpl, cfg, now=now)
    res.findings += f
    if res.status == "fehler":
        res.bundle = None if any(x.code.startswith(("META_", "FHIR_", "ABS_VOLL")) and x.severity == FEHLER
                                 for x in res.findings) else res.bundle
    return res


# --------------------------------------------------------------------------
# Ablage und Zustellung
# --------------------------------------------------------------------------

def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")[:80] or "dokument"


def _atomic(path: Path, data: bytes) -> None:
    """Erst unter anderem Namen schreiben, dann umbenennen: Abholer sehen nie halbe Dateien."""
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def deliver(res: Result, cfg: dict, ziel: str | None = None, trotz_pruefung: bool = False) -> None:
    z = cfg.get("zustellung") or {}
    ziel = ziel or z.get("ziel")
    if not res.bundle or res.status == "fehler":
        res.delivery = "nicht zugestellt: Fehler, siehe oben"
        return
    if not ziel:
        res.delivery = "kein Zustellziel eingestellt; Bundle liegt im Ausgabeordner"
        return
    if res.status == "pruefen" and not (trotz_pruefung or z.get("bei_pruefen") == "zustellen"):
        res.delivery = (f"zurückgehalten, weil Punkte zu prüfen sind (Ziel wäre {ziel}). "
                        "Nach Prüfung mit --trotz-pruefung zustellen")
        return
    payload = json.dumps(res.bundle, ensure_ascii=False, indent=1).encode("utf-8")
    fname = _safe(res.doc.meta.doc_id) + ".json"
    try:
        if ziel.startswith(("http://", "https://")):
            req = urllib.request.Request(ziel, data=payload, method="POST",
                                         headers={"Content-Type": "application/fhir+json; charset=utf-8"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                res.delivery = f"per HTTP an {ziel} übergeben (Antwort {resp.status})"
        else:
            target = Path(ziel[5:] if ziel.startswith("file:") else ziel)
            target.mkdir(parents=True, exist_ok=True)
            _atomic(target / fname, payload)
            res.delivery = f"abgelegt in {target / fname}"
        res.delivered = True
    except urllib.error.HTTPError as exc:
        res.delivery = f"Zustellung fehlgeschlagen: {ziel} antwortet mit HTTP {exc.code} {exc.read().decode('utf-8', 'replace')[:160]}"
    except (OSError, urllib.error.URLError) as exc:
        res.delivery = f"Zustellung fehlgeschlagen: {getattr(exc, 'reason', exc)}"
    if not res.delivered:
        res.findings.append(Finding("ZUSTELLUNG", FEHLER, "Zustellung", res.delivery[0].upper() + res.delivery[1:] + ".",
                                    "Ziel prüfen und erneut zustellen. Das Bundle liegt im Ausgabeordner."))


def write_outputs(res: Result, outdir: str | os.PathLike) -> Path:
    """Legt je Dokument einen Ordner an: Bundle, Bericht, Original, gelesener Text."""
    from .report import render_html, to_json
    m = res.doc.meta
    folder = Path(outdir) / _safe(m.doc_id or Path(res.doc.name).stem)
    folder.mkdir(parents=True, exist_ok=True)
    if res.bundle:
        _atomic(folder / "bundle.json", json.dumps(res.bundle, ensure_ascii=False, indent=1).encode("utf-8"))
        res.files["bundle"] = str(folder / "bundle.json")
    elif (folder / "bundle.json").exists():
        (folder / "bundle.json").unlink()                       # kein veraltetes Bundle liegen lassen
    if res.doc.original:
        ext = {"application/pdf": "pdf", "x-application/hl7-v2+er7": "hl7"}.get(res.doc.original_type, "txt")
        (folder / f"original.{ext}").write_bytes(res.doc.original)
        res.files["original"] = str(folder / f"original.{ext}")
        res.files["original_sha256"] = hashlib.sha256(res.doc.original).hexdigest()
    (folder / "text.txt").write_text("\n".join(ln.text for ln in res.doc.lines), encoding="utf-8")
    res.files["text"] = str(folder / "text.txt")
    res.files["bericht"] = str(folder / "bericht.html")
    res.files["bericht_json"] = str(folder / "bericht.json")
    (folder / "bericht.json").write_text(json.dumps(to_json(res), ensure_ascii=False, indent=1), encoding="utf-8")
    (folder / "bericht.html").write_text(render_html(res), encoding="utf-8")
    return folder


__all__ = ["Result", "ingest", "process", "deliver", "write_outputs", "make_llm", "sniff", "INFO", "PRUEFEN"]
