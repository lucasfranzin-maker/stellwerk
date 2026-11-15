"""Workbench: Projekte als Baum, Aufgaben mit Quelle, Zuordnung und FHIR-Ausgabe.

Gespeichert wird in der Datenbank (siehe db.py). Quelldateien und Bundles enthalten Patientendaten.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import fieldmap
from .db import Store
from .model import FEHLER
from .pdftext import _ICD_VERLESEN
from .pipeline import _atomic, _safe, ingest, make_llm, process
from .report import METHODE, STATUS_SATZ
from .sections import list_templates, load_template, norm, ocr_key
from .textrepair import decode_bytes

_ID = re.compile(r"^[a-z0-9]{6,32}$")
_META = ("patient_id", "encounter_id", "doc_id", "doc_date", "author", "title", "family", "given", "birth_date")
KANAL_VORGABE = {"intervall": 60, "neu_spalte": "", "neu_art": "zahl", "vollabfrage": False, "start": "", "max_zeilen": 5000,
                 "zustellen": [], "jdbc_url": ""}
VORGABE = {"art": "datei", "vorlage": "", "meta": {}, "aliase": {}, "db": "", "abfrage": "", "dokument": 0, "ablage": "",
           "modus": "dokument", "felder": {"regeln": {}, "abgelehnt": [], "status": ""}, "kanal": KANAL_VORGABE}


def clean_kanal(k) -> dict:
    """Einstellungen für den BridgeLink-Kanal prüfen. Wirft ApiError, wenn etwas nicht stimmt."""
    k = {**KANAL_VORGABE, **(k if isinstance(k, dict) else {})}
    try:
        intervall, max_zeilen = int(k["intervall"]), int(k["max_zeilen"])
    except (TypeError, ValueError) as exc:
        raise ApiError(400, "Intervall und Zeilen je Lauf müssen ganze Zahlen sein.") from exc
    if not 5 <= intervall <= 86400:
        raise ApiError(400, "Das Intervall liegt zwischen 5 Sekunden und einem Tag.")
    if not 1 <= max_zeilen <= 100000:
        raise ApiError(400, "Zeilen je Lauf: 1 bis 100000.")
    spalte = str(k["neu_spalte"] or "").strip()
    if spalte and not re.match(r"^[A-Za-z_][A-Za-z0-9_ .$-]{0,100}$", spalte):
        raise ApiError(400, "Der Spaltenname für neue Zeilen enthält unzulässige Zeichen.")
    if k["neu_art"] not in ("zahl", "zeit"):
        raise ApiError(400, "Die Art des Kriteriums für neue Zeilen muss zahl oder zeit sein.")
    start = str(k["start"] or "").strip()
    if start and not re.match(r"^-?\d+(?:\.\d+)?$|^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?$", start):
        raise ApiError(400, "Der Startwert ist keine Zahl und kein Zeitstempel (JJJJ-MM-TT HH:MM:SS).")
    jdbc = str(k["jdbc_url"] or "").strip()
    if jdbc and (not jdbc.startswith("jdbc:") or "password=" in jdbc.lower() or "pwd=" in jdbc.lower()):
        raise ApiError(400, "Die JDBC-Adresse muss mit jdbc: beginnen und darf kein Passwort enthalten.")
    return {"intervall": intervall, "neu_spalte": spalte, "neu_art": k["neu_art"], "vollabfrage": bool(k["vollabfrage"]), "start": start,
            "max_zeilen": max_zeilen, "zustellen": [c for c in dict.fromkeys(k["zustellen"] or []) if c in ("TEXTWERT", "GESCHLECHT", "DATUM")],
            "jdbc_url": jdbc}


class ApiError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


class Workspace:
    def __init__(self, store: Store):
        self.store = store

    # --- Baum
    def tree(self) -> dict:
        return {"kinder": self.store.tree()}

    def save_tree(self, tree: dict) -> set[str]:
        seen: set[str] = set()

        def clean(node: dict, depth: int) -> dict:
            nid = str(node.get("id", ""))
            if depth > 12 or not _ID.match(nid) or node.get("typ") not in ("ordner", "aufgabe") or nid in seen:
                raise ApiError(400, "Ungültiger Eintrag im Baum.")
            seen.add(nid)
            name = str(node.get("name", "")).strip()[:120] or "Ohne Namen"
            out = {"id": nid, "name": name, "typ": node["typ"]}
            if node["typ"] == "ordner":
                out["kinder"] = [clean(k, depth + 1) for k in node.get("kinder") or []]
                out["offen"] = bool(node.get("offen", True))
            return out
        return self.store.replace_tree([clean(k, 0) for k in tree.get("kinder") or []])

    # --- Aufgabe
    @staticmethod
    def check(tid: str) -> str:
        if not _ID.match(tid):
            raise ApiError(400, "Ungültige Aufgaben-Kennung.")
        return tid

    def task(self, tid: str) -> dict:
        row = self.store.task(self.check(tid))
        t = {**VORGABE, **((row.einstellungen or {}) if row else {})}
        t["quelle"] = row.quelle_name if row and row.quelle is not None else ""
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
        t["kanal"] = clean_kanal(t["kanal"])
        from .sqlsource import has_password
        if has_password(str(t["db"])):
            raise ApiError(400, "Die Datenbankadresse enthält ein Passwort. Bitte als Platzhalter ${STELLWERK_DB_…} angeben "
                                "und das Passwort als Umgebungsvariable des Dienstes setzen.")
        self.store.save_task(self.check(tid), einstellungen=t)
        return self.task(tid)

    def save_source(self, tid: str, name: str, data: bytes) -> None:
        if not data:
            raise ApiError(400, "Leere Datei.")
        self.store.save_task(self.check(tid), quelle=data, quelle_name=_safe(name), bundle=None)

    def bundle(self, tid: str) -> bytes | None:
        row = self.store.task(self.check(tid))
        return row.bundle if row else None

    def _set_bundle(self, tid: str, data: bytes | None) -> None:
        self.store.save_task(tid, bundle=data)

    def _record(self, tid: str, art: str, status: str, findings: list, t0: float, name: str = "") -> None:
        self.store.add_run(kanal="workbench", aufgabe_id=tid, quelle=name[:120], art=art, status=status,
                           fehler=sum(1 for f in findings if f["stufe"] == FEHLER),
                           pruefen=sum(1 for f in findings if f["stufe"] == "pruefen"),
                           dauer_ms=int((time.monotonic() - t0) * 1000), codes=sorted({f["code"] for f in findings}))

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
        t0 = time.monotonic()
        t = self.task(tid)
        cols, rows = self._query(t)
        fd = t["felder"]
        bundles, findings, labels = fieldmap.build(cols, rows, fd["regeln"], cfg, fd["status"])
        samples = {c: list(dict.fromkeys(str(r[i]) for r in rows if r[i] not in (None, "")))[:4] for i, c in enumerate(cols)}
        prof = fieldmap.profile(cols, rows)
        llm, note = make_llm(cfg)
        vor = fieldmap.suggest(cols, fd["regeln"], fd["abgelehnt"], llm, samples if llm and llm.patientendaten else None)
        n = min(max(t["dokument"], 0), max(len(rows) - 1, 0))
        bundle = bundles[n] if bundles else None
        fehler = any(x.severity == FEHLER for x in findings)
        self._set_bundle(tid, fieldmap.dump_json(bundle, indent=1).encode("utf-8") if bundle and not fehler else None)
        status = "fehler" if fehler else "pruefen" if any(x.severity == "pruefen" for x in findings) or vor else "ok"
        out = {
            "modus": "felder", "status": status, "satz": STATUS_SATZ[status], "modell": llm.label if llm else note, "art": "sql",
            "dokumente": labels, "dokument": n, "bloecke": [], "kopf": {}, "vorlage": {"name": "Feld-Mapping", "abschnitte": []},
            "roh": {"spalten": cols, "zeilen": [["" if c is None else str(c)[:300] for c in r] for r in rows[:50]], "gesamt": len(rows)},
            "ziele": [{"key": k, "pfad": p, "text": x} for k, p, x in fieldmap.ZIELE],
            "spalten": [{"name": c, "beispiele": samples[c], "regel": fd["regeln"].get(c), "vorschlag": vor.get(c),
                         "abgelehnt": c in fd["abgelehnt"], "profil": prof[c]} for c in cols],
            "feldstatus": fd["status"], "bundles": sum(1 for b in bundles if b),
            "befunde": [{"code": x.code, "stufe": x.severity, "wo": x.where, "meldung": x.message, "was_tun": x.action,
                         "detail": x.detail} for x in findings],
            "bundle": bundle if not fehler else None,
            "bundle_text": fieldmap.dump_json(bundle, indent=2) if bundle and not fehler else "",
        }
        self._record(tid, "sql", status, out["befunde"], t0, "Feld-Mapping")
        return out

    def run(self, tid: str, cfg: dict) -> dict:
        t0 = time.monotonic()
        t = self.task(tid)
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
            row = self.store.task(tid)
            if not row or row.quelle is None:
                raise ApiError(400, "Noch keine Quelldatei hochgeladen.")
            docs = ingest(row.quelle, t["quelle"], cfg, t["meta"])
        n = min(max(t["dokument"], 0), len(docs) - 1)
        doc = docs[n]
        llm, note = make_llm(cfg)
        res = process(doc, cfg, llm=llm, llm_note=note, tpl=tpl)
        if t["art"] != "sql":
            if doc.kind == "hl7":
                roh = {"text": decode_bytes(doc.stats.get("eingang") or b"")[0].replace("\r\n", "\n").replace("\r", "\n")}
            else:
                roh = {"text": "\n".join(ln.text for ln in doc.lines)}
        self._set_bundle(tid, json.dumps(res.bundle, ensure_ascii=False, indent=1).encode("utf-8")
                         if res.bundle and res.status != "fehler" else None)
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
        out = {
            "status": res.status, "satz": STATUS_SATZ[res.status], "modell": res.llm, "art": doc.kind,
            "dokumente": [x.name for x in docs], "dokument": n, "roh": roh, "bloecke": bloecke,
            "vorlage": {"name": tpl.name, "abschnitte": [{"key": a["key"], "titel": a["titel"]} for a in tpl.abschnitte]},
            "kopf": {k: getattr(m, k) for k in _META},
            "befunde": [{"code": f.code, "stufe": f.severity, "wo": f.where, "meldung": f.message, "was_tun": f.action,
                         "detail": f.detail} for f in res.findings],
            "bundle": res.bundle if not any(f.severity == FEHLER for f in res.findings) else None,
        }
        self._record(tid, doc.kind, res.status, out["befunde"], t0, "Abfrage" if t["art"] == "sql" else "Datei")   # Dateinamen können Patientennamen enthalten
        return out

    def send(self, tid: str, cfg: dict) -> dict:
        """Bundle(s) der Aufgabe an das eingestellte Zustellziel (z. B. BridgeLink) übergeben."""
        import urllib.error

        from .bridgelink import routing_headers
        from .pipeline import send_http
        z = cfg.get("zustellung") or {}
        ziel = str(z.get("ziel") or "")
        if not ziel.startswith(("http://", "https://")):
            raise ApiError(400, "Kein HTTP-Zustellziel eingestellt (zustellung.ziel in stellwerk.yaml).")
        t = self.task(tid)
        if t["art"] == "sql" and t["modus"] == "felder":
            cols, rows = self._query(t)
            bundles, findings, _ = fieldmap.build(cols, rows, t["felder"]["regeln"], cfg, t["felder"]["status"])
            if any(x.severity == FEHLER for x in findings):
                raise ApiError(409, "Kein zustellbares Bundle. Erst die Fehler beheben.")
            status = "pruefen" if any(x.severity == "pruefen" for x in findings) else "ok"
            payloads = [fieldmap.dump_json(b).encode("utf-8") for b in bundles if b]
            quelle = "Feld-Mapping"
        else:
            data = self.bundle(tid)
            if not data:
                raise ApiError(409, "Kein zustellbares Bundle. Erst ausführen und Fehler beheben.")
            last = self.store.runs(1, tid)
            status = last[0]["status"] if last else "pruefen"
            payloads, quelle = [data], ("Abfrage" if t["art"] == "sql" else "Datei")
        n = 0
        try:
            for p in payloads:
                send_http(ziel, p, z, routing_headers(json.loads(p), status, quelle))
                n += 1
        except urllib.error.HTTPError as exc:
            raise ApiError(502, f"{ziel} antwortet mit HTTP {exc.code} nach {n} von {len(payloads)} Bundles.") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise ApiError(502, f"Übergabe an {ziel} fehlgeschlagen nach {n} von {len(payloads)} Bundles: "
                                f"{getattr(exc, 'reason', exc)}") from exc
        return {"ziel": ziel, "bundles": n, "status": status}

    def export(self, tid: str, ordner: str, cfg: dict | None = None) -> dict:
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
        data = self.bundle(tid)
        if not data:
            raise ApiError(409, "Kein zustellbares Bundle. Erst ausführen und Fehler beheben.")
        if not ordner.strip():
            raise ApiError(400, "Abholordner eintragen.")
        target = Path(ordner)
        try:
            target.mkdir(parents=True, exist_ok=True)
            bundle = json.loads(data.decode("utf-8"))
            comp = next(e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Composition")
            name = _safe(comp["identifier"]["value"]) + ".json"
            _atomic(target / name, data)
        except OSError as exc:
            raise ApiError(400, f"Ablage fehlgeschlagen: {exc}") from exc
        return {"datei": str(target / name)}


def discover(ws: Workspace, cfg: dict, db: str, entscheidungen: dict | None) -> dict:
    from . import discovery
    if not db:
        raise ApiError(400, "Datenbankadresse eintragen.")
    from .sqlsource import public_url
    key = public_url(db)                                         # nie mit Passwort speichern
    ent = ws.store.decisions(key)
    neu = {str(k): str(v) for k, v in (entscheidungen or {}).items() if v in discovery.BEDEUTUNGEN or v == discovery.UNGEKLAERT}
    if neu:
        ent.update(neu)
        ws.store.save_decisions(key, ent)
    llm, _ = make_llm(cfg)
    try:
        schema = discovery.crawl(db, with_values=True)
    except discovery.DiscoveryError as exc:
        raise ApiError(400, str(exc)) from exc
    res = discovery.analyse(schema, ent, llm)
    res["abfragen"] = discovery.propose_queries(res)
    discovery.verify_queries(db, res, res["abfragen"])
    return res
