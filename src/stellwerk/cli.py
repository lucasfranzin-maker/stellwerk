"""Kommandozeile. Rückgabewert: 0 = OK, 1 = zu prüfen, 2 = Fehler."""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

import yaml

from . import __version__, config
from .pipeline import deliver, ingest, make_llm, process, write_outputs
from .report import render_text, to_json
from .sections import list_templates, load_template

_CODE = {"ok": 0, "pruefen": 1, "fehler": 2}


def _fail(msg: str) -> int:
    print(f"FEHLER  {msg}", file=sys.stderr)
    return 2


def _finish(results, args, cfg) -> int:
    for res in results:
        deliver(res, cfg, getattr(args, "zustellen", None), getattr(args, "trotz_pruefung", False))
        write_outputs(res, args.out or cfg["ausgabe"])
    if getattr(args, "json", False):
        print(json.dumps([to_json(r) for r in results], ensure_ascii=False, indent=1))
    else:
        skip = set()
        if len(results) > 1:
            keys = [{(f.code, f.message) for f in r.findings} for r in results]
            skip = set.intersection(*keys)
            gemeinsam = [f for f in results[0].findings if (f.code, f.message) in skip]
            if gemeinsam:
                print(f"Gilt für alle {len(results)} Dokumente")
                for f in gemeinsam:
                    art = {"fehler": "Fehler", "pruefen": "Prüfen", "info": "Erledigt"}[f.severity]
                    print(f"  {art:<8} [{f.where}] {f.message}" + (f"\n           → {f.action}" if f.action and f.severity != "info" else ""))
                print("─" * 96)
        print(("\n" + "─" * 96 + "\n").join(render_text(r, skip) for r in results), end="")
        if len(results) > 1:
            n = {s: sum(1 for r in results if r.status == s) for s in _CODE}
            print(f"\nZusammen: {len(results)} Dokumente, {n['ok']} OK, {n['pruefen']} zu prüfen, {n['fehler']} mit Fehler.")
    return max((_CODE[r.status] for r in results), default=2)


def cmd_convert(args, cfg) -> int:
    if args.ocr:
        cfg["ocr"]["modus"] = args.ocr
    if args.ohne_llm:
        cfg["llm"]["modell"] = ""
    meta = {"patient_id": args.patient_id, "encounter_id": args.fall, "doc_id": args.dokument_id, "author": args.autor,
            "title": args.titel, "family": args.name, "given": args.vorname, "birth_date": args.geburtsdatum, "doc_date": args.datum}
    llm, note = make_llm(cfg)
    results = []
    for f in args.dateien:
        p = Path(f)
        if not p.is_file():
            return _fail(f"Datei nicht gefunden: {p}")
        for doc in ingest(p.read_bytes(), p.name, cfg, meta):
            results.append(process(doc, cfg, args.vorlage, llm, note))
    return _finish(results, args, cfg)


def cmd_sql(args, cfg) -> int:
    from .sqlsource import SqlError, docs_from_rows, read_csv, run_query
    mp = Path(args.zuordnung)
    if not mp.is_file():
        return _fail(f"Zuordnungsdatei nicht gefunden: {mp}")
    mapping = yaml.safe_load(mp.read_text(encoding="utf-8")) or {}
    q = mapping.get("quelle") or {}
    tpl = load_template(args.vorlage or mapping.get("vorlage") or cfg["vorlage"])
    pre = []
    try:
        if q.get("csv"):
            cp = (mp.parent / q["csv"]) if not Path(q["csv"]).is_absolute() else Path(q["csv"])
            cols, rows, pre = read_csv(cp.read_bytes(), cp.name)
            name = cp.name
        else:
            query = q.get("abfrage") or ((mp.parent / q["abfrage_datei"]).read_text(encoding="utf-8") if q.get("abfrage_datei") else "")
            db = args.db or q.get("db") or ""
            if not db or not query:
                return _fail("In der Zuordnungsdatei fehlen quelle.db und quelle.abfrage (oder quelle.csv).")
            if db.startswith("sqlite:///") and not Path(db[10:]).is_absolute():
                db = "sqlite:///" + str(mp.parent / db[10:])
            cols, rows = run_query(db, query, args.limit)
            name = mp.stem
    except (SqlError, OSError) as exc:
        return _fail(str(exc))
    if args.ohne_llm:
        cfg["llm"]["modell"] = ""
    llm, note = make_llm(cfg)
    results = [process(d, cfg, tpl.pfad, llm, note)
               for d in docs_from_rows(cols, rows, mapping, tpl, cfg, name, pre)]
    return _finish(results, args, cfg)


def cmd_doctor(args, cfg) -> int:
    from .pdftext import ocr_available
    bad = 0

    def line(ok, name, text, hint=""):
        nonlocal bad
        bad += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'} {name:<16} {text}" + (f"\n    → {hint}" if hint and not ok else ""))

    print(f"Stellwerk {__version__}")
    print(f"  ✓ Konfiguration    {cfg.get('_datei') or 'keine stellwerk.yaml gefunden, Vorgaben aktiv'}")
    unset = [k for k, v in cfg["systeme"].items() if not v]
    line(not unset, "Namensräume", "eingestellt" if not unset else f"fehlen für: {', '.join(unset)}",
         "In stellwerk.yaml unter systeme eintragen. Bis dahin stehen Platzhalter im Bundle und jedes Dokument ist „zu prüfen“.")
    line(True, "Vorlagen", ", ".join(list_templates()))
    ok, why = ocr_available(cfg["ocr"]["sprache"])
    line(ok, "Texterkennung", f"tesseract mit Sprache {cfg['ocr']['sprache']}" if ok else why,
         "apt install tesseract-ocr tesseract-ocr-deu poppler-utils; pip install 'stellwerk[ocr]'. Ohne das gehen nur PDFs mit Textebene.")
    llm, note = make_llm(cfg)
    if not llm:
        if not cfg["llm"].get("modell"):
            print("  – Lokales Modell   nicht eingestellt. Stellwerk arbeitet dann nur mit den Überschriften der Vorlage.\n"
                  "    → Optional: llm.modell und llm.url in stellwerk.yaml setzen (Ollama oder OpenAI-kompatibler Server).")
        else:
            line(False, "Lokales Modell", note)
    else:
        try:
            models = llm.models()
            known = any(mm == llm.model or mm.split(":")[0] == llm.model for mm in models)
            line(known, "Lokales Modell", f"{llm.label} erreichbar" if known else f"Server erreichbar, aber Modell „{llm.model}“ fehlt",
                 f"Vorhanden: {', '.join(models) or 'keine'}. Modell laden, z. B. ollama pull {llm.model}")
            if known:
                ok, text = llm.selftest()
                line(ok, "Modelltest", text, "Das Modell ordnet die Testtexte nicht zuverlässig zu. Ein größeres Modell wählen.")
        except Exception as exc:
            line(False, "Lokales Modell", str(exc), "Läuft der Server? Bei Ollama: ollama serve")
    ziel = cfg["zustellung"].get("ziel")
    print(f"  {'✓' if ziel else '–'} Zustellung       {ziel or 'kein Ziel eingestellt; Bundles bleiben im Ausgabeordner'}")
    return 2 if bad else 0


def cmd_serve(args, cfg) -> int:
    from .server import serve
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("ACHTUNG  Der Dienst hat keine Anmeldung und verarbeitet Patientendaten. "
              "Nur in einem geschützten Netz oder hinter einem Reverse-Proxy mit Anmeldung betreiben.", file=sys.stderr)
    srv = serve(args.host, args.port, cfg, args.out or cfg["ausgabe"])
    print(f"Stellwerk läuft auf http://{args.host}:{args.port}  (Seite zum Ausprobieren: /, Schnittstelle: POST /v1/convert)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_mllp(args, cfg) -> int:
    from .mllp import serve
    print(f"Stellwerk empfängt HL7 v2 per MLLP auf {args.host}:{args.port}. Ausgabe nach {args.out or cfg['ausgabe']}.")
    try:
        asyncio.run(serve(args.host, args.port, cfg, args.out or cfg["ausgabe"], args.vorlage))
    except KeyboardInterrupt:
        pass
    return 0


def cmd_init(args, cfg) -> int:
    target = Path("stellwerk.yaml")
    if target.exists():
        return _fail("stellwerk.yaml gibt es hier schon; nichts überschrieben.")
    from importlib import resources
    shutil.copyfile(str(resources.files("stellwerk") / "stellwerk.beispiel.yaml"), target)
    print("stellwerk.yaml angelegt. Als Nächstes die Namensräume unter systeme eintragen, dann: stellwerk doctor")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="stellwerk", description="HL7 v2, SQL und PDF in FHIR-Compositions überführen.")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--config", help="Pfad zur Konfiguration (Vorgabe: ./stellwerk.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, deliver_opts=True):
        p.add_argument("--vorlage", help=f"Vorlage: {', '.join(list_templates())} oder Pfad zu einer YAML-Datei")
        p.add_argument("--out", help="Ausgabeordner (Vorgabe: out)")
        if deliver_opts:
            p.add_argument("--zustellen", metavar="ZIEL", help="Ordner oder http(s)-Adresse, an die das Bundle geht")
            p.add_argument("--trotz-pruefung", action="store_true", help="auch zustellen, wenn Punkte zu prüfen sind")
            p.add_argument("--ohne-llm", action="store_true", help="lokales Modell für diesen Lauf nicht benutzen")
            p.add_argument("--json", action="store_true", help="Bericht als JSON statt als Text ausgeben")

    c = sub.add_parser("convert", help="HL7-, PDF- oder Textdateien umwandeln")
    c.add_argument("dateien", nargs="+")
    common(c)
    c.add_argument("--ocr", choices=["auto", "immer", "nie"], help="Texterkennung (Vorgabe: auto)")
    for flag, text in (("--patient-id", "Patientennummer"), ("--fall", "Fallnummer"), ("--dokument-id", "Dokumentnummer"),
                       ("--datum", "Dokumentdatum, z. B. 2026-11-13"), ("--autor", "Verfasser"), ("--titel", "Titel"),
                       ("--name", "Nachname"), ("--vorname", "Vorname"), ("--geburtsdatum", "Geburtsdatum, z. B. 1964-08-12")):
        c.add_argument(flag, help=f"{text} (für PDF und Text, die keine Kopfdaten mitbringen)")
    c.set_defaults(fn=cmd_convert)

    s = sub.add_parser("sql", help="Dokumente aus einer SQL-Abfrage oder CSV-Datei umwandeln")
    s.add_argument("zuordnung", help="YAML-Datei mit Quelle und Spaltenzuordnung")
    s.add_argument("--db", help="Datenbankadresse, überschreibt quelle.db")
    s.add_argument("--limit", type=int, help="höchstens so viele Zeilen lesen")
    common(s)
    s.set_defaults(fn=cmd_sql)

    v = sub.add_parser("vorlage", help="mitgelieferte Vorlage ausgeben, um sie anzupassen")
    v.add_argument("name", choices=list_templates())
    v.set_defaults(fn=lambda a, c: print((__import__("importlib.resources").resources.files("stellwerk") / "vorlagen" / f"{a.name}.yaml").read_text(encoding="utf-8"), end="") or 0)
    d = sub.add_parser("doctor", help="Einrichtung prüfen: Texterkennung, lokales Modell, Konfiguration")
    d.set_defaults(fn=cmd_doctor)
    i = sub.add_parser("init", help="stellwerk.yaml mit kommentierten Vorgaben anlegen")
    i.set_defaults(fn=cmd_init)
    h = sub.add_parser("serve", help="HTTP-Dienst mit Seite zum Ausprobieren starten")
    h.add_argument("--host", default="127.0.0.1")
    h.add_argument("--port", type=int, default=8080)
    common(h, False)
    h.set_defaults(fn=cmd_serve)
    m = sub.add_parser("mllp", help="HL7 v2 per MLLP empfangen")
    m.add_argument("--host", default="127.0.0.1")
    m.add_argument("--port", type=int, default=6661)
    common(m, False)
    m.set_defaults(fn=cmd_mllp)

    args = ap.parse_args(argv)
    try:
        cfg = config.load(args.config)
        return args.fn(args, cfg)
    except FileNotFoundError as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    sys.exit(main())
