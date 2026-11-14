"""Workbench: Projekte als Baum, Aufgaben mit Quelle, Zuordnung und FHIR-Ausgabe.

Ablage auf Platte im Arbeitsbereich: baum.json und je Aufgabe ein Ordner mit
aufgabe.json, der Quelldatei und dem letzten Bundle. Der Arbeitsbereich enthält Patientendaten.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import fieldmap
from .model import FEHLER
from .pdftext import _ICD_VERLESEN
from .pipeline import _atomic, _safe, ingest, make_llm, process
from .report import METHODE, STATUS_SATZ
from .sections import list_templates, load_template, norm, ocr_key
from .textrepair import decode_bytes

_ID = re.compile(r"^[a-z0-9]{6,32}$")
_META = ("patient_id", "encounter_id", "doc_id", "doc_date", "author", "title", "family", "given", "birth_date")
VORGABE = {"art": "datei", "vorlage": "", "meta": {}, "aliase": {}, "db": "", "abfrage": "", "dokument": 0, "ablage": "",
           "modus": "dokument", "felder": {"regeln": {}, "abgelehnt": [], "status": ""}}


class ApiError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


class Workspace:
    def __init__(self, root: str):
        self.root = Path(root)

    # --- Baum
    def tree(self) -> dict:
        p = self.root / "baum.json"
        return json.loads(p.read_text("utf-8")) if p.exists() else {"kinder": []}

    def save_tree(self, tree: dict) -> None:
        def clean(node: dict, depth: int) -> dict:
            if depth > 12 or not _ID.match(str(node.get("id", ""))) or node.get("typ") not in ("ordner", "aufgabe"):
                raise ApiError(400, "Ungültiger Eintrag im Baum.")
            name = str(node.get("name", "")).strip()[:120] or "Ohne Namen"
            out = {"id": node["id"], "name": name, "typ": node["typ"]}
            if node["typ"] == "ordner":
                out["kinder"] = [clean(k, depth + 1) for k in node.get("kinder") or []]
                out["offen"] = bool(node.get("offen", True))
            return out
        data = {"kinder": [clean(k, 0) for k in tree.get("kinder") or []]}
        self.root.mkdir(parents=True, exist_ok=True)
        _atomic(self.root / "baum.json", json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))

    # --- Aufgabe
    def dir(self, tid: str) -> Path:
        if not _ID.match(tid):
            raise ApiError(400, "Ungültige Aufgaben-Kennung.")
        d = self.root / "aufgaben" / tid
        d.mkdir(parents=True, exist_ok=True)
        return d

    def task(self, tid: str) -> dict:
        p = self.dir(tid) / "aufgabe.json"
        t = {**VORGABE, **(json.loads(p.read_text("utf-8")) if p.exists() else {})}
        src = self.dir(tid) / "quelle.name"
        t["quelle"] = src.read_text("utf-8") if src.exists() else ""
        return t

    def save_task(self, tid: str, data: dict) -> dict:
        t = {k: data.get(k, v) for k, v in VORGABE.items()}
        if t["art"] not in ("datei", "sql"):
            raise ApiError(400, "art muss datei oder sql sein.")
        t["meta"] = {k: str(v).strip() for k, v in (t["meta"] or {}).items() if k in _META and str(v).strip()}
        t["aliase"] = {str(k)[:120]: str(v) for k, v in (t["aliase"] or {}).items()}
        t["dokument"] = int(t["dokument"] or 0)
        if t["modus"] not in ("dokument", "felder"):
            raise ApiError(400, "modus muss dokument oder felder sein.")
        fd = t["felder"] or {}
        keys = {k for k, _, _ in fieldmap.ZIELE}
        t["felder"] = {
            "regeln": {str(c)[:120]: {k: str(v)[:200] for k, v in r.items() if k in ("ziel", "loinc", "anzeige", "fuer", "einheit")}
                       for c, r in (fd.get("regeln") or {}).items() if isinstance(r, dict) and r.get("ziel") in keys},
            "abgelehnt": [str(c)[:120] for c in fd.get("abgelehnt") or []],
            "status": str(fd.get("status") or "")}
        _atomic(self.dir(tid) / "aufgabe.json", json.dumps(t, ensure_ascii=False, indent=1).encode("utf-8"))
        return self.task(tid)

    def save_source(self, tid: str, name: str, data: bytes) -> None:
        if not data:
            raise ApiError(400, "Leere Datei.")
        d = self.dir(tid)
        (d / "quelle.bin").write_bytes(data)
        (d / "quelle.name").write_text(_safe(name), "utf-8")

    # --- Lauf
    def _query(self, t: dict):
        from .sqlsource import SqlError, run_query
        if not t["db"] or not t["abfrage"].strip():
            raise ApiError(400, "Datenbankadresse und Abfrage eintragen.")
        try:
            return run_query(t["db"], t["abfrage"], 500)
        except SqlError as exc:
            raise ApiError(400, str(exc)) from exc

    def run_fields(self, tid: str, cfg: dict) -> dict:
        """Feld-Mapping: Spalten -> Patient und Observation."""
        t = self.task(tid)
        d = self.dir(tid)
        cols, rows = self._query(t)
        fd = t["felder"]
        bundles, findings, labels = fieldmap.build(cols, rows, fd["regeln"], cfg, fd["status"])
        samples = {c: list(dict.fromkeys(str(r[i]) for r in rows if r[i] not in (None, "")))[:4] for i, c in enumerate(cols)}
        llm, note = make_llm(cfg)
        vor = fieldmap.suggest(cols, fd["regeln"], fd["abgelehnt"], llm, samples)
        n = min(max(t["dokument"], 0), max(len(rows) - 1, 0))
        bundle = bundles[n] if bundles else None
        fehler = any(x.severity == FEHLER for x in findings)
        if bundle and not fehler:
            _atomic(d / "bundle.json", fieldmap.dump_json(bundle, indent=1).encode("utf-8"))
        elif (d / "bundle.json").exists():
            (d / "bundle.json").unlink()
        status = "fehler" if fehler else "pruefen" if any(x.severity == "pruefen" for x in findings) or vor else "ok"
        return {
            "modus": "felder", "status": status, "satz": STATUS_SATZ[status], "modell": llm.label if llm else note, "art": "sql",
            "dokumente": labels, "dokument": n, "bloecke": [], "kopf": {}, "vorlage": {"name": "Feld-Mapping", "abschnitte": []},
            "roh": {"spalten": cols, "zeilen": [["" if c is None else str(c)[:300] for c in r] for r in rows[:50]], "gesamt": len(rows)},
            "ziele": [{"key": k, "pfad": p, "text": x} for k, p, x in fieldmap.ZIELE],
            "spalten": [{"name": c, "beispiele": samples[c], "regel": fd["regeln"].get(c), "vorschlag": vor.get(c),
                         "abgelehnt": c in fd["abgelehnt"]} for c in cols],
            "feldstatus": fd["status"], "bundles": sum(1 for b in bundles if b),
            "befunde": [{"code": x.code, "stufe": x.severity, "wo": x.where, "meldung": x.message, "was_tun": x.action,
                         "detail": x.detail} for x in findings],
            "bundle": bundle if not fehler else None,
            "bundle_text": fieldmap.dump_json(bundle, indent=2) if bundle and not fehler else "",
        }

    def run(self, tid: str, cfg: dict) -> dict:
        t = self.task(tid)
        d = self.dir(tid)
        if t["art"] == "sql" and t["modus"] == "felder":
            return self.run_fields(tid, cfg)
        try:
            tpl = load_template(t["vorlage"] or cfg.get("vorlage", "entlassbrief"))
        except FileNotFoundError as exc:
            raise ApiError(400, str(exc)) from exc
        for heading, key in t["aliase"].items():
            if key in tpl.by_key:
                tpl.alias[norm(heading)] = key
                tpl._ocr[ocr_key(heading)] = key
        roh: dict = {}
        if t["art"] == "sql":
            from .sqlsource import docs_from_rows
            cols, rows = self._query(t)
            docs = docs_from_rows(cols, rows, {}, tpl, cfg, "Abfrage")
            for doc in docs:
                for k, v in t["meta"].items():
                    setattr(doc.meta, k, v)
            roh = {"spalten": cols, "zeilen": [["" if c is None else str(c)[:300] for c in r] for r in rows[:50]], "gesamt": len(rows)}
        else:
            src = d / "quelle.bin"
            if not src.exists():
                raise ApiError(400, "Noch keine Quelldatei hochgeladen.")
            docs = ingest(src.read_bytes(), t["quelle"], cfg, t["meta"])
        n = min(max(t["dokument"], 0), len(docs) - 1)
        doc = docs[n]
        llm, note = make_llm(cfg)
        res = process(doc, cfg, llm=llm, llm_note=note, tpl=tpl)
        if t["art"] != "sql":
            if doc.kind == "hl7":
                roh = {"text": decode_bytes(doc.stats.get("eingang") or b"")[0].replace("\r\n", "\n").replace("\r", "\n")}
            else:
                roh = {"text": "\n".join(ln.text for ln in doc.lines)}
        if res.bundle and res.status != "fehler":
            _atomic(d / "bundle.json", json.dumps(res.bundle, ensure_ascii=False, indent=1).encode("utf-8"))
        elif (d / "bundle.json").exists():
            (d / "bundle.json").unlink()
        bloecke = []
        for s in res.sections:
            for p in s.parts:
                bloecke.append({
                    "abschnitt": s.key, "titel": s.title, "ueberschrift": p.heading, "methode": p.method,
                    "methode_text": METHODE[p.method], "sicherheit": p.note,
                    "zeilen": [{"text": ln.text, "herkunft": ln.origin, "ocr": ln.conf, "unsicher": ln.uncertain,
                                "pruefen": bool(ln.conf is not None and ln.text.strip()
                                                and (ln.conf < 80 or ln.uncertain or _ICD_VERLESEN.search(ln.text)))}
                               for ln in p.lines]})
        m = doc.meta
        return {
            "status": res.status, "satz": STATUS_SATZ[res.status], "modell": res.llm, "art": doc.kind,
            "dokumente": [x.name for x in docs], "dokument": n, "roh": roh, "bloecke": bloecke,
            "vorlage": {"name": tpl.name, "abschnitte": [{"key": a["key"], "titel": a["titel"]} for a in tpl.abschnitte]},
            "kopf": {k: getattr(m, k) for k in _META},
            "befunde": [{"code": f.code, "stufe": f.severity, "wo": f.where, "meldung": f.message, "was_tun": f.action,
                         "detail": f.detail} for f in res.findings],
            "bundle": res.bundle if not any(f.severity == FEHLER for f in res.findings) else None,
        }

    def export(self, tid: str, ordner: str, cfg: dict | None = None) -> dict:
        d = self.dir(tid)
        t = self.task(tid)
        if t["art"] == "sql" and t["modus"] == "felder":
            if not ordner.strip():
                raise ApiError(400, "Abholordner eintragen.")
            cols, rows = self._query(t)
            bundles, findings, _ = fieldmap.build(cols, rows, t["felder"]["regeln"], cfg or {}, t["felder"]["status"])
            if any(x.severity == FEHLER for x in findings):
                raise ApiError(409, "Kein zustellbares Bundle. Erst die Fehler beheben.")
            target = Path(ordner)
            try:
                target.mkdir(parents=True, exist_ok=True)
                n = 0
                for i, b in enumerate(bundles, 1):
                    if b:
                        _atomic(target / f"{_safe(tid)}-zeile-{i:05d}.json", fieldmap.dump_json(b, indent=1).encode("utf-8"))
                        n += 1
            except OSError as exc:
                raise ApiError(400, f"Ablage fehlgeschlagen: {exc}") from exc
            return {"datei": f"{n} Bundles in {target}"}
        if not (d / "bundle.json").exists():
            raise ApiError(409, "Kein zustellbares Bundle. Erst ausführen und Fehler beheben.")
        if not ordner.strip():
            raise ApiError(400, "Abholordner eintragen.")
        target = Path(ordner)
        try:
            target.mkdir(parents=True, exist_ok=True)
            bundle = json.loads((d / "bundle.json").read_text("utf-8"))
            comp = next(e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Composition")
            name = _safe(comp["identifier"]["value"]) + ".json"
            _atomic(target / name, (d / "bundle.json").read_bytes())
        except OSError as exc:
            raise ApiError(400, f"Ablage fehlgeschlagen: {exc}") from exc
        return {"datei": str(target / name)}


def handle(ws: Workspace, cfg: dict, method: str, path: str, query: dict, body: bytes):
    """Kleine JSON-Schnittstelle der Workbench. Liefert (Status, dict oder bytes)."""
    parts = [p for p in path.split("/") if p][1:]              # ohne "api"

    def js():
        try:
            return json.loads(body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError(400, "Kein gültiges JSON.") from exc

    if parts == ["baum"]:
        if method == "PUT":
            ws.save_tree(js())
        return 200, {**ws.tree(), "vorlagen": list_templates()}
    if len(parts) >= 2 and parts[0] == "aufgabe":
        tid, rest = parts[1], parts[2:]
        if not rest:
            if method == "PUT":
                return 200, ws.save_task(tid, js())
            return 200, ws.task(tid)
        if rest == ["quelle"] and method == "POST":
            ws.save_source(tid, query.get("name", "quelle"), body)
            return 200, ws.task(tid)
        if rest == ["lauf"] and method == "POST":
            return 200, ws.run(tid, cfg)
        if rest == ["ablegen"] and method == "POST":
            return 200, ws.export(tid, js().get("ordner", ""), cfg)
        if rest == ["bundle"] and method == "GET":
            p = ws.dir(tid) / "bundle.json"
            if not p.exists():
                raise ApiError(404, "Kein Bundle vorhanden.")
            return 200, p.read_bytes()
    raise ApiError(404, "Unbekannter Pfad.")
