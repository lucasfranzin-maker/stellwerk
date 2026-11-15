"""Kommandozeile. Rückgabewert: 0 = OK, 1 = zu prüfen, 2 = Fehler."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
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


def cmd_discover(args, cfg) -> int:
    from . import discovery
    try:
        schema = discovery.crawl(args.db, with_values=not args.ohne_werte)
    except discovery.DiscoveryError as exc:
        return _fail(str(exc))
    llm, _ = make_llm(cfg)
    res = discovery.analyse(schema, llm=llm)
    res["abfragen"] = discovery.propose_queries(res)
    discovery.verify_queries(args.db, res, res["abfragen"])
    if args.abfragen:
        print(discovery.render_queries(res["abfragen"]), end="")
        return 1 if any(a["pruefen"] for a in res["abfragen"]) else 0
    print(json.dumps(res, ensure_ascii=False, indent=1) if args.json else discovery.render_text(res), end="")
    return 1 if res["zusammenfassung"]["ungeklaert"] or res["zusammenfassung"]["unsicher"] else 0


def cmd_uebergabe(args, cfg) -> int:
    from . import uebergabe
    from .db import Store
    from .workbench import ApiError, Workspace
    store = Store(cfg["datenbank"])
    store.migrate()
    ws = Workspace(store)
    if not args.aufgabe:
        def zeigen(nodes, pfad):
            for n in nodes:
                if n["typ"] == "aufgabe":
                    print(f"{n['id']:<14} {' / '.join(pfad + [n['name']])}")
                zeigen(n.get("kinder") or [], pfad + [n["name"]])
        zeigen(ws.tree()["kinder"], [])
        return 0
    try:
        name, data = uebergabe.build_package(ws, args.aufgabe, cfg, args.mit_daten)
    except ApiError as exc:
        return _fail(str(exc))
    ziel = Path(args.out or name)
    ziel.write_bytes(data)
    store.audit("kommandozeile", "uebergabe.erzeugt", args.aufgabe, "mit echten Zeilen (Patientendaten)" if args.mit_daten else "ohne Patientendaten")
    print(f"Übergabepaket geschrieben: {ziel}" + ("  (enthält Patientendaten)" if args.mit_daten else ""))
    return 0


def cmd_schnittstelle(args, cfg) -> int:
    from . import schnittstelle
    from .db import Store
    from .workbench import ApiError, Workspace
    store = Store(cfg["datenbank"])
    store.migrate()
    ws = Workspace(store)
    if not args.aufgabe:
        def zeigen(nodes, pfad):
            for n in nodes:
                if n["typ"] == "aufgabe":
                    print(f"{n['id']:<14} {' / '.join(pfad + [n['name']])}")
                zeigen(n.get("kinder") or [], pfad + [n["name"]])
        zeigen(ws.tree()["kinder"], [])
        return 0
    try:
        k = schnittstelle.felder_kanal(ws, args.aufgabe, cfg)
        for h in k.hinweise:
            print("Hinweis:", h, file=sys.stderr)
        if args.einrichten:
            was = schnittstelle.einrichten(k, cfg)
            store.audit("kommandozeile", "schnittstelle.eingerichtet", args.aufgabe, f"{was}, Kanal {k.id}")
            print(f"Kanal „{k.name}“ {was} und bereitgestellt (ID {k.id}).")
            return 0
    except ApiError as exc:
        return _fail(str(exc))
    ziel = Path(args.out or f"kanal-{k.id[:8]}.xml")
    fd = os.open(ziel, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)          # enthält das Datenbankpasswort
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(k.xml)
    store.audit("kommandozeile", "schnittstelle.erzeugt", args.aufgabe, f"Kanal {k.id}")
    print(f"Kanal geschrieben: {ziel}. In BridgeLink unter „Kanäle“ importieren.")
    return 0


def cmd_vergleichen(args, cfg) -> int:
    from . import uebergabe
    try:
        soll, ist = (uebergabe.load_bundle(Path(p).read_bytes()) for p in (args.erwartet, args.geliefert))
    except (OSError, ValueError) as exc:
        return _fail(f"Datei nicht lesbar: {exc}")
    unterschiede = uebergabe.compare(soll, ist)
    if not unterschiede:
        print("Gleich. Reihenfolge der Einträge und fullUrl wurden nicht verglichen.")
        return 0
    print(f"{len(unterschiede)} Abweichung(en):")
    for u in unterschiede:
        print("  " + u)
    return 1


def cmd_doctor(args, cfg) -> int:
    from .pdftext import ocr_available
    bad = 0

    def line(ok, name, text, hint=""):
        nonlocal bad
        bad += 0 if ok else 1
        print(f"  {'✓' if ok else '✗'} {name:<16} {text}" + (f"\n    → {hint}" if hint and not ok else ""))

    print(f"Stellwerk {__version__}")
    print(f"  ✓ Konfiguration    {cfg.get('_datei') or 'keine stellwerk.yaml gefunden, Vorgaben aktiv'}")
    unset = [k for k, v in cfg["systeme"].items() if not v and k != "auftrag"]     # auftrag braucht nur das Feld-Mapping
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
    import uvicorn

    from .app import create_app
    if args.out:
        cfg["ausgabe"] = args.out
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("ACHTUNG  Der Dienst hat noch keine Anmeldung und verarbeitet Patientendaten. "
              "Nur in einem geschützten Netz oder hinter einem Reverse-Proxy mit Anmeldung betreiben.", file=sys.stderr)
    app = create_app(cfg)
    print(f"Stellwerk läuft auf http://{args.host}:{args.port}  (Datenbank: {app.state.store.engine.dialect.name})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False, proxy_headers=True,
                forwarded_allow_ips=os.environ.get("STELLWERK_PROXY_IPS", "127.0.0.1"), server_header=False)
    return 0


def cmd_mllp(args, cfg) -> int:
    from .mllp import serve
    print(f"Stellwerk empfängt HL7 v2 per MLLP auf {args.host}:{args.port}. Ausgabe nach {args.out or cfg['ausgabe']}.")
    try:
        from .db import Store
        store = Store(cfg["datenbank"])
        store.migrate()
        asyncio.run(serve(args.host, args.port, cfg, args.out or cfg["ausgabe"], args.vorlage, store=store))
    except KeyboardInterrupt:
        pass
    return 0


def cmd_bridgelink(args, cfg) -> int:
    from . import bridgelink
    b, z = cfg.get("bridgelink") or {}, cfg.get("zustellung") or {}
    k = b.get("kanal") or {}
    try:
        user = z.get("benutzer") or ""
        pw = os.environ.get(z.get("passwort_env") or "STELLWERK_ZIEL_PASSWORT", "")
        if not user:
            return _fail("zustellung.benutzer fehlt. Der Kanal nimmt nur angemeldete Anfragen an.")
        if args.aktion == "kanal":
            print(bridgelink.channel_xml(k, user, pw)[1])
            return 0
        client = bridgelink.BridgeLink(b)
        version = client.version()
        cid, xml = bridgelink.channel_xml(k, user, pw, version)
        if args.aktion == "einrichten":
            was = client.install(cid, xml)
            print(f"Kanal „{k.get('name')}“ {was} und bereitgestellt (BridgeLink {version}, ID {cid}).")
            soll = f":{k.get('port')}{k.get('pfad')}"
            if not str(z.get("ziel") or "").endswith(soll):
                print(f"Noch einstellen: zustellung.ziel = http(s)://<bridgelink-host>{soll}")
            if not k.get("fhir_ziel"):
                print("Hinweis: kanal.fhir_ziel ist leer. Alle Bundles landen in der Ablage im BridgeLink-Server.")
            return 0
        st = client.status(cid)
        print(f"{st['name'] or k.get('name')}: {st['zustand']}. Empfangen {st['empfangen']}, "
              f"abgewiesen {st['abgewiesen']} (ohne Routing-Merkmal).")
        for d in st["ziele"]:
            print(f"  {d['name']}: gesendet {d['gesendet']}, Fehler {d['fehler']}, in Warteschlange {d['warteschlange']}")
        return 2 if st["fehler"] or st["zustand"] != "STARTED" else 0
    except bridgelink.BridgeLinkError as exc:
        return _fail(str(exc))


def cmd_migrate(args, cfg) -> int:
    from .db import Store
    if args.sql:
        print(Store.migration_sql(args.sql), end="")
        return 0
    st = Store(cfg["datenbank"])
    st.migrate()
    print(f"Datenbankschema ist aktuell ({st.engine.dialect.name}).")
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
    ap = argparse.ArgumentParser(prog="stellwerk", description="Schnittstellen ins FHIR-CDR entwerfen: HL7 v2, SQL und PDF in FHIR überführen.")
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
    e = sub.add_parser("discover", help="Datenbankschema lesen, Bedeutung der Felder und Zusammenhänge vorschlagen")
    e.add_argument("db", help="Datenbankadresse, z. B. sqlite:///kis.db")
    e.add_argument("--json", action="store_true", help="Ergebnis als JSON ausgeben")
    e.add_argument("--ohne-werte", action="store_true", help="nur das Schema lesen, keine Zeilen")
    e.add_argument("--abfragen", action="store_true", help="fertige SELECT-Vorschläge ausgeben (mit Verknüpfung zu Fall und Patient)")
    e.set_defaults(fn=cmd_discover)
    ue = sub.add_parser("uebergabe", help="Übergabepaket einer Aufgabe für den Kommunikationsserver schreiben")
    ue.add_argument("aufgabe", nargs="?", help="Kennung der Aufgabe; ohne Angabe werden die Aufgaben aufgelistet")
    ue.add_argument("--out", help="Zieldatei (Vorgabe: uebergabe-<Name>.zip)")
    ue.add_argument("--mit-daten", action="store_true", help="echte Zeilen als zusätzliche Testfälle beilegen (Patientendaten)")
    ue.set_defaults(fn=cmd_uebergabe)
    sc = sub.add_parser("schnittstelle", help="Fertigen BridgeLink-Kanal für eine Feld-Mapping-Aufgabe erzeugen oder einrichten")
    sc.add_argument("aufgabe", nargs="?", help="Kennung der Aufgabe; ohne Angabe werden die Aufgaben aufgelistet")
    sc.add_argument("--einrichten", action="store_true", help="Kanal in BridgeLink anlegen und bereitstellen (statt eine Datei zu schreiben)")
    sc.add_argument("--out", help="Zieldatei für das Kanal-XML (Vorgabe: kanal-<Kennung>.xml, nur für den Besitzer lesbar)")
    sc.set_defaults(fn=cmd_schnittstelle)
    vg = sub.add_parser("vergleichen", help="geliefertes Bundle mit dem erwarteten aus einem Übergabepaket vergleichen")
    vg.add_argument("erwartet", help="erwartet.json aus dem Paket")
    vg.add_argument("geliefert", help="Bundle, das der Kommunikationsserver erzeugt hat")
    vg.set_defaults(fn=cmd_vergleichen)
    d = sub.add_parser("doctor", help="Einrichtung prüfen: Texterkennung, lokales Modell, Konfiguration")
    d.set_defaults(fn=cmd_doctor)
    bl = sub.add_parser("bridgelink", help="BridgeLink-Kanal ausgeben, einrichten oder prüfen")
    bl.add_argument("aktion", choices=["kanal", "einrichten", "status"],
                    help="kanal: Kanal-XML ausgeben | einrichten: anlegen und bereitstellen | status: Zustand und Zähler")
    bl.set_defaults(fn=cmd_bridgelink)
    mg = sub.add_parser("migrate", help="Datenbankschema anlegen oder auf den neuesten Stand bringen")
    mg.add_argument("--sql", metavar="DIALEKT", choices=["postgresql", "sqlite"], help="nur das SQL ausgeben, z. B. für die Datenbankverwaltung")
    mg.set_defaults(fn=cmd_migrate)
    i = sub.add_parser("init", help="stellwerk.yaml mit kommentierten Vorgaben anlegen")
    i.set_defaults(fn=cmd_init)
    h = sub.add_parser("serve", help="Workbench und HTTP-Schnittstelle starten")
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
