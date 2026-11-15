"""BridgeLink-Kanal je Schnittstelle: Aufbau, Prüfungen, Skripte, Regeln. Gegen einen echten BridgeLink siehe docs/bridgelink.md."""
import io
import json
import shutil
import sqlite3
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from stellwerk import schnittstelle, uebergabe
from stellwerk.cli import main
from stellwerk.db import Store
from stellwerk.workbench import ApiError, Workspace, clean_kanal

NODE = shutil.which("node")
REGELN = {"pat_id": {"ziel": "patient.id"}, "fall_nr": {"ziel": "fall.id"}, "auftrag_nr": {"ziel": "auftrag.id"},
          "geb_datum": {"ziel": "patient.birthDate"}, "geschlecht": {"ziel": "patient.gender"}, "nachname": {"ziel": "patient.family"},
          "vorname": {"ziel": "patient.given"}, "abnahme_zeit": {"ziel": "obs.zeit"}, "lfd_nr": {"ziel": "ignorieren"},
          "krea_wert": {"ziel": "obs.wert", "loinc": "2160-0"}, "krea_einheit": {"ziel": "obs.einheit", "fuer": "krea_wert"},
          "crp": {"ziel": "obs.wert", "loinc": "1988-5", "einheit": "mg/l"}, "bemerkung": {"ziel": "ignorieren"}, "fallart": {"ziel": "ignorieren"}}
SQL = """SELECT l.lfd_nr, l.auftrag_nr, l.fall_nr, l.abnahme_zeit, l.krea_wert, l.krea_einheit, l.crp, l.bemerkung, f.pat_id, f.fallart,
  p.nachname, p.vorname, p.geb_datum, p.geschlecht
FROM LABORBEFUND l LEFT JOIN FALL f ON f.fall_nr = l.fall_nr LEFT JOIN PATIENT p ON p.pat_id = f.pat_id"""


@pytest.fixture
def lis(bsp, tmp_path):
    """Kopie der Beispiel-Datenbank mit laufender Nummer (die Beispieldatei selbst bleibt unverändert)."""
    ziel = tmp_path / "lis.db"
    shutil.copy(bsp / "lis.db", ziel)
    con = sqlite3.connect(ziel)
    con.execute("ALTER TABLE LABORBEFUND ADD COLUMN lfd_nr INTEGER")
    con.execute("UPDATE LABORBEFUND SET lfd_nr = rowid")
    con.commit()
    con.close()
    return ziel


@pytest.fixture
def cfg2(cfg, tmp_path):
    cfg["bridgelink"]["kanal"].update(fhir_ziel="https://cdr.haus/fhir", pruefablage=str(tmp_path / "pruefen"), stand_ordner=str(tmp_path / "stand"))
    cfg["systeme"]["auftrag"] = "https://haus.example/sid/auftrag"
    return cfg


@pytest.fixture
def ws(tmp_path):
    store = Store(f"sqlite:///{tmp_path / 'ws.db'}")
    store.migrate()
    w = Workspace(store)
    w.save_tree({"kinder": [{"id": "task000001", "name": "Laborwerte LIS", "typ": "aufgabe"}]})
    return w


def aufgabe(ws, lis, **kanal):
    ws.save_task("task000001", {"art": "sql", "modus": "felder", "db": f"sqlite:///{lis}", "abfrage": SQL,
                                "felder": {"regeln": REGELN, "abgelehnt": [], "status": "final"},
                                "kanal": {"neu_spalte": "lfd_nr", **kanal}})


def skripte(xml):
    root = ET.fromstring(xml)
    q = root.find("sourceConnector/properties")
    return {"select": q.findtext("select"), "update": q.findtext("update"),
            "filter": root.find("sourceConnector/filter//script").text}


# --------------------------------------------------------------------------

def test_kanal_hat_alle_bausteine(ws, lis, cfg2, tmp_path):
    aufgabe(ws, lis, intervall=30, max_zeilen=250, start="100")
    k = schnittstelle.felder_kanal(ws, "task000001", cfg2)
    root = ET.fromstring(k.xml)
    assert root.findtext("name") == "Stellwerk Laborwerte LIS" and root.findtext("id") == k.id
    q = root.find("sourceConnector/properties")
    assert root.findtext("sourceConnector/transportName") == "Database Reader" and q.findtext("driver") == "org.sqlite.JDBC"
    assert q.findtext("url") == f"jdbc:sqlite:{lis}" and q.findtext("useScript") == "true" and q.findtext("updateMode") == "3"
    assert q.findtext("pollConnectorProperties/pollingFrequency") == "30000"
    s = skripte(k.xml)
    assert '"spalte": "lfd_nr"' in s["select"] and "setMaxRows(K.max)" in s["select"] and '"max": 250' in s["select"] and '"start": "100"' in s["select"]
    assert "LABORBEFUND l LEFT JOIN FALL f" in s["select"] and s["select"].count("stellwerk_q") == 1
    assert str(tmp_path / "stand" / "Laborwerte-LIS.stand") in s["update"] and "resultMap.get(K.spalte" in s["update"]
    assert "StellwerkBauen" in s["filter"] and '"zustellen": []' in s["filter"] and "KEINE_PATIENTENNUMMER" in s["filter"]
    ziele = root.findall("destinationConnectors/connector")
    assert [z.findtext("name") for z in ziele] == ["FHIR-Ziel", "Prüfablage", "Prüfhinweise"]
    assert [z.findtext("transportName") for z in ziele] == ["HTTP Sender", "File Writer", "File Writer"]
    assert ziele[0].findtext("properties/host") == "https://cdr.haus/fhir" and ziele[0].findtext("properties/destinationConnectorProperties/queueEnabled") == "true"
    assert ziele[1].findtext("properties/host") == str(tmp_path / "pruefen" / "Laborwerte-LIS")
    assert [m.findtext("name") for m in root.findall("properties/metaDataColumns/metaDataColumn")] == ["STATUS", "GRUND", "HINWEISE"]
    pr = root.find("exportData/metadata/pruningSettings")
    assert (pr.findtext("pruneContentDays"), pr.findtext("pruneMetaDataDays"), pr.findtext("archiveEnabled")) == ("7", "30", "false")
    assert any("Neue Zeilen: Spalte „lfd_nr“" in h for h in k.hinweise) and any("offene Punkte" in h for h in k.hinweise)
    assert k.id == schnittstelle.felder_kanal(ws, "task000001", cfg2).id                  # stabile Kennung: erneutes Einrichten ersetzt den Kanal


def test_ohne_fhir_ziel_gibt_es_nur_eine_ablage(ws, lis, cfg2):
    cfg2["bridgelink"]["kanal"]["fhir_ziel"] = ""
    aufgabe(ws, lis)
    k = schnittstelle.felder_kanal(ws, "task000001", cfg2)
    assert [z.findtext("name") for z in ET.fromstring(k.xml).findall("destinationConnectors/connector")] == ["Ablage", "Prüfhinweise"]
    assert any("fhir_ziel ist leer" in h for h in k.hinweise)


def test_gewaehlte_hinweise_gehen_direkt_ans_ziel(ws, lis, cfg2):
    aufgabe(ws, lis, zustellen=["TEXTWERT", "gibtsnicht"])
    assert '"zustellen": ["TEXTWERT"]' in skripte(schnittstelle.felder_kanal(ws, "task000001", cfg2).xml)["filter"]


@pytest.mark.skipif(not NODE, reason="node ist nicht installiert")
def test_skripte_sind_gueltiges_javascript(ws, lis, cfg2, tmp_path):
    aufgabe(ws, lis)
    for name, text in skripte(schnittstelle.felder_kanal(ws, "task000001", cfg2).xml).items():
        f = tmp_path / f"{name}.js"
        f.write_text("(function () {\n" + text + "\n});\n", encoding="utf-8")        # Skripte von BridgeLink stehen in einer Funktion (return erlaubt)
        r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, f"{name}: {r.stderr}"


@pytest.mark.skipif(not NODE, reason="node ist nicht installiert")
def test_kanal_baut_jedes_bundle_des_pakets(ws, lis, cfg2, tmp_path):
    """Dieselbe Datei, die im Kanal läuft (kanal_bauen.js), muss für jeden Testfall des Übergabepakets das erwartete Bundle bauen."""
    aufgabe(ws, lis)
    _, data = uebergabe.build_package(ws, "task000001", cfg2, True)
    zipfile.ZipFile(io.BytesIO(data)).extractall(tmp_path / "paket")
    root = next((tmp_path / "paket").iterdir())
    ist = json.loads(subprocess.run([NODE, str(Path(__file__).parent / "kanal_nachbau.js"), str(root)], capture_output=True, text=True, check=True).stdout)
    faelle = sorted(p.parent.relative_to(root).as_posix() for p in root.glob("*/*/zeile.json"))
    assert len(faelle) == 11 and sorted(ist) == faelle
    for f in faelle:
        soll = uebergabe.load_bundle((root / f / "erwartet.json").read_text(encoding="utf-8"))
        assert uebergabe.compare(soll, uebergabe.load_bundle(ist[f])) == [], f


@pytest.mark.skipif(not NODE, reason="node ist nicht installiert")
def test_zeile_aus_dem_xml_des_database_readers():
    xml = ('<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n<result>\n  <auftrag_nr>L&amp;1</auftrag_nr>\n  <crp>&lt;0,6</crp>\n'
           '  <leer></leer>\n  <nachname>Müller &#228;&#x44;</nachname>\n</result>\n')
    js = ("const B=require(process.argv[1]);process.stdout.write(JSON.stringify(B.zeileLesen(process.argv[2])))")
    row = json.loads(subprocess.run([NODE, "-e", js, str(Path(__file__).parent.parent / "src/stellwerk/kanal_bauen.js"), xml],
                                    capture_output=True, text=True, check=True).stdout)
    assert row == {"auftrag_nr": "L&1", "crp": "<0,6", "leer": "", "nachname": "Müller äD"}     # fehlende Spalten (NULL) stehen nicht im XML


# --------------------------------------------------------------------------

@pytest.mark.parametrize("kanal, text", [
    ({"neu_spalte": ""}, "festlegen, woran neue Zeilen erkannt werden"),
    ({"neu_spalte": "gibtsnicht"}, "steht nicht im Ergebnis"),
    ({"neu_spalte": "abnahme_zeit"}, "keine Zahl"),
    ({"neu_spalte": "abnahme_zeit", "neu_art": "zeit"}, "SQLite speichert Zeitangaben als Text"),
    ({"neu_spalte": "", "vollabfrage": True, "_ohne_auftrag": True}, "Ohne Auftragsnummer"),
])
def test_ohne_verlaessliches_kriterium_kein_kanal(ws, lis, cfg2, kanal, text):
    ohne = kanal.pop("_ohne_auftrag", False)
    aufgabe(ws, lis, **kanal)
    if ohne:
        t = ws.task("task000001")
        t["felder"]["regeln"].pop("auftrag_nr")
        ws.save_task("task000001", t)
    with pytest.raises(ApiError, match=text) as e:
        schnittstelle.felder_kanal(ws, "task000001", cfg2)
    assert e.value.code == 409


def test_alle_zeilen_nur_mit_auftragsnummer(ws, lis, cfg2):
    aufgabe(ws, lis, neu_spalte="", vollabfrage=True)
    k = schnittstelle.felder_kanal(ws, "task000001", cfg2)
    assert ET.fromstring(k.xml).findtext("sourceConnector/properties/updateMode") == "1" and '"spalte": ""' in skripte(k.xml)["select"]
    assert any("Alle Zeilen werden bei jedem Lauf gelesen" in h for h in k.hinweise)


def test_fragezeichen_in_der_abfrage_ist_ein_platzhalter(ws, lis, cfg2):
    aufgabe(ws, lis)
    t = ws.task("task000001")
    t["abfrage"] = SQL + " WHERE l.bemerkung <> '?'"
    ws.save_task("task000001", t)
    with pytest.raises(ApiError, match="Fragezeichen"):
        schnittstelle.felder_kanal(ws, "task000001", cfg2)


def test_nur_feld_mapping_aus_datenbank(ws, lis, cfg2):
    aufgabe(ws, lis)
    t = ws.task("task000001")
    t["modus"] = "dokument"
    ws.save_task("task000001", t)
    with pytest.raises(ApiError, match="kein Feld-Mapping"):
        schnittstelle.felder_kanal(ws, "task000001", cfg2)


def test_zeitzonen_in_der_quelle_werden_abgelehnt():
    from datetime import datetime, timezone
    r = {"abnahme_zeit": {"ziel": "obs.zeit"}, "geb": {"ziel": "patient.birthDate"}, "x": {"ziel": "ignorieren"}}
    ok = schnittstelle._pruefe_zeitzonen(["abnahme_zeit", "geb", "x"], [(datetime(2026, 1, 1, 8), "1964-08-12", datetime.now(timezone.utc))], r)
    assert ok == []
    schlecht = schnittstelle._pruefe_zeitzonen(["abnahme_zeit", "geb", "x"], [(datetime(2026, 1, 1, 8, tzinfo=timezone.utc), "1964-08-12", None)], r)
    assert len(schlecht) == 1 and "abnahme_zeit" in schlecht[0] and "AT TIME ZONE" in schlecht[0]


def test_jdbc_adressen(monkeypatch, tmp_path):
    monkeypatch.setenv("STELLWERK_DB_PW", "geheim")
    pg = schnittstelle.jdbc("postgresql+psycopg://stw:${STELLWERK_DB_PW}@db.haus:5433/lis")
    assert pg == {"art": "postgresql", "treiber": "org.postgresql.Driver", "url": "jdbc:postgresql://db.haus:5433/lis", "benutzer": "stw", "passwort": "geheim"}
    assert schnittstelle.jdbc("mssql+pyodbc://u:${STELLWERK_DB_PW}@kis/KIS")["url"] == "jdbc:sqlserver://kis:1433;databaseName=KIS"
    assert schnittstelle.jdbc("oracle+oracledb://u:${STELLWERK_DB_PW}@ora:1521/?service_name=ORCL")["url"] == "jdbc:oracle:thin:@//ora:1521/ORCL"
    assert schnittstelle.jdbc("mysql+pymysql://u:${STELLWERK_DB_PW}@m/db")["url"] == "jdbc:mysql://m:3306/db"
    assert schnittstelle.jdbc(f"sqlite:///{tmp_path}/a.db")["url"] == f"jdbc:sqlite:{tmp_path}/a.db"
    assert schnittstelle.jdbc("postgresql+psycopg://stw:${STELLWERK_DB_PW}@h/d", "jdbc:postgresql://h/d?ssl=true")["url"] == "jdbc:postgresql://h/d?ssl=true"
    with pytest.raises(ApiError, match="absoluten Pfad"):
        schnittstelle.jdbc("sqlite:///relativ.db")
    with pytest.raises(ApiError, match="kennt Stellwerk keine JDBC-Adresse"):
        schnittstelle.jdbc("firebird://u:${STELLWERK_DB_PW}@h/d")


def test_einstellungen_werden_geprueft():
    assert clean_kanal(None)["neu_art"] == "zahl" and clean_kanal({"zustellen": ["DATUM", "DATUM", "x"]})["zustellen"] == ["DATUM"]
    for schlecht, text in (({"intervall": 1}, "zwischen 5 Sekunden"), ({"intervall": "x"}, "ganze Zahlen"), ({"max_zeilen": 0}, "Zeilen je Lauf"),
                           ({"neu_spalte": "a; DROP TABLE x"}, "unzulässige Zeichen"), ({"neu_art": "text"}, "zahl oder zeit"),
                           ({"start": "gestern"}, "Startwert"), ({"jdbc_url": "mysql://x"}, "mit jdbc: beginnen"),
                           ({"jdbc_url": "jdbc:postgresql://h/d?password=x"}, "kein Passwort")):
        with pytest.raises(ApiError, match=text):
            clean_kanal(schlecht)
    assert clean_kanal({"start": "2026-11-14 12:00:00.123"})["start"] == "2026-11-14 12:00:00.123"


# --------------------------------------------------------------------------

@pytest.fixture
def labor(api, lis):
    api("PUT", "baum", {"kinder": [{"id": "task000001", "name": "Laborwerte LIS", "typ": "aufgabe"}]})
    api("PUT", "aufgabe/task000001", {"art": "sql", "modus": "felder", "db": f"sqlite:///{lis}", "abfrage": SQL,
                                      "felder": {"regeln": REGELN, "abgelehnt": [], "status": "final"}, "kanal": {"neu_spalte": "lfd_nr"}})
    return api


def test_api_prueft_und_liefert_nie_das_kanal_xml(labor, client):
    r = client.get("/api/aufgabe/task000001/kanal")
    assert r.status_code == 200 and "<channel" not in r.text and "password" not in r.text.lower()
    j = r.json()
    assert j["bereit"] is True and j["name"] == "Stellwerk Laborwerte LIS" and j["bridgelink"] is False and j["hinweise"]
    t = labor("GET", "aufgabe/task000001")
    t["kanal"]["neu_spalte"] = ""
    labor("PUT", "aufgabe/task000001", t)
    j = client.get("/api/aufgabe/task000001/kanal").json()
    assert j["bereit"] is False and "woran neue Zeilen erkannt werden" in j["meldung"]


def test_api_einrichten_braucht_bridgelink(labor):
    with pytest.raises(ApiError, match="BridgeLink ist nicht eingerichtet") as e:
        labor("POST", "aufgabe/task000001/kanal")
    assert e.value.code == 409


def test_kommandozeile_schreibt_den_kanal_nur_fuer_den_besitzer(ws, lis, cfg2, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STELLWERK_DATENBANK", f"sqlite:///{tmp_path / 'ws.db'}")
    aufgabe(ws, lis)
    assert main(["schnittstelle"]) == 0 and "task000001" in capsys.readouterr().out
    ziel = tmp_path / "kanal.xml"
    assert main(["schnittstelle", "task000001", "--out", str(ziel)]) == 0
    assert ET.fromstring(ziel.read_text(encoding="utf-8")).findtext("name") == "Stellwerk Laborwerte LIS" and ziel.stat().st_mode & 0o777 == 0o600
    assert main(["schnittstelle", "task000009"]) == 2
    assert main(["schnittstelle", "task000001", "--einrichten"]) == 2              # ohne bridgelink.api: Fehler statt Absturz


def test_aufbewahrungsfristen_werden_geprueft(ws, lis, cfg2):
    aufgabe(ws, lis)
    cfg2["bridgelink"]["kanal"].update(inhalt_tage=None, metadaten_tage="")
    assert ET.fromstring(schnittstelle.felder_kanal(ws, "task000001", cfg2).xml).find("exportData/metadata/pruningSettings/pruneContentDays") is None
    for wert in (0, 99999, "x"):
        cfg2["bridgelink"]["kanal"]["inhalt_tage"] = wert
        with pytest.raises(ApiError, match="inhalt_tage"):
            schnittstelle.felder_kanal(ws, "task000001", cfg2)


def test_vorschlag_nennt_nur_vergleichbare_spalten_als_kriterium(tmp_path):
    """Der Kanal lehnt Text als Kriterium ab; die Abfragevorschläge dürfen ihn deshalb nicht empfehlen."""
    from stellwerk import discovery
    con = sqlite3.connect(tmp_path / "x.db")
    con.execute("CREATE TABLE LABORBEFUND (lfd_nr INTEGER PRIMARY KEY, auftrag_nr TEXT, pat_id TEXT, abnahme_zeit TIMESTAMP, krea_wert TEXT, krea_einheit TEXT)")
    con.executemany("INSERT INTO LABORBEFUND VALUES (?,?,?,?,?,?)", [(i, f"L-{i}", "4711", f"2026-11-0{i} 08:00:00", "1,1", "mg/dl") for i in range(1, 5)])
    con.commit()
    con.close()
    res = discovery.analyse(discovery.crawl(f"sqlite:///{tmp_path / 'x.db'}"))
    (a,) = discovery.propose_queries(res)
    hinweis = next(h for h in a["hinweise"] if "laufenden Betrieb" in h)
    assert "lfd_nr (laufende Nummer)" in hinweis and "abnahme_zeit (Zeitstempel)" in hinweis and "auftrag_nr" not in hinweis


# --- dauerhafte Ablehnung durch das CDR, Data Pruner ---------------------------------------------------------------------

_HARNESS = """\
const skript = require('fs').readFileSync(process.argv[2], 'utf8'), faelle = JSON.parse(process.argv[3]);
const ergebnis = [];
for (const f of faelle) {
  const geschrieben = {};
  const java = { io: {
    File: function (a, b) { this.p = b === undefined ? a : a.p + '/' + b; this.mkdirs = function () {}; this.renameTo = (t) => { geschrieben[t.p] = geschrieben[this.p]; delete geschrieben[this.p]; }; },
    FileOutputStream: function (file) { this.f = file; },
    OutputStreamWriter: function (o) { this.write = (t) => { geschrieben[o.f.p] = t; }; this.close = function () {}; } } };
  const karte = (o) => ({ get: (k) => (k in o ? o[k] : null) });
  const msg = { getMessageId: () => 77, getEncodedData: () => 'AUS-DER-NACHRICHT' };
  const fn = new Function('connectorMap', 'channelMap', 'connectorMessage', 'java', 'ERROR', 'responseStatus', 'responseStatusMessage',
                          skript + '\\n;return [responseStatus, responseStatusMessage];');
  const [st, text] = fn(karte(f.connector), karte(f.channel), msg, java, 'ERROR', 'QUEUED', '');
  ergebnis.push({ status: st, text, dateien: geschrieben });
}
console.log(JSON.stringify(ergebnis));
"""


@pytest.mark.skipif(not NODE, reason="node ist nicht installiert")
def test_ablehnung_nimmt_nur_dauerhafte_antworten_aus_der_warteschlange(tmp_path):
    from stellwerk import bridgelink as bl
    skript = tmp_path / "ablehnung.js"
    skript.write_text(bl.ablehnung_skript({}, "/ablage/lis/"), encoding="utf-8")
    harness = tmp_path / "h.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    codes = [200, 201, 400, 401, 403, 404, 408, 409, 413, 415, 422, 429, 500, 503, 0]
    faelle = [{"connector": {"responseStatusCode": c} if c else {}, "channel": {"stellwerkBundle": '{"resourceType":"Bundle"}'}} for c in codes]
    faelle.append({"connector": {"responseStatusCode": 422}, "channel": {}})                      # Eingangskanal: Bundle nur in der Nachricht
    r = subprocess.run([NODE, str(harness), str(skript), json.dumps(faelle)], capture_output=True, text=True, check=True)
    erg = json.loads(r.stdout)
    for c, e in zip(codes, erg):
        if c in (400, 413, 415, 422):
            assert e["status"] == "ERROR" and f"HTTP {c}" in e["text"], c
            assert set(e["dateien"]) == {"/ablage/lis/abgelehnt/77.json", "/ablage/lis/abgelehnt/77.abgelehnt.txt"}
            assert e["dateien"]["/ablage/lis/abgelehnt/77.json"] == '{"resourceType":"Bundle"}'
            assert "Fehlerprotokoll" in e["dateien"]["/ablage/lis/abgelehnt/77.abgelehnt.txt"]
        else:
            assert e["status"] == "QUEUED" and e["dateien"] == {}, c                # Ausfall, Anmeldung, Zeitüberschreitung: wiederholen
    assert erg[-1]["dateien"]["/ablage/lis/abgelehnt/77.json"] == "AUS-DER-NACHRICHT"


def test_ablehnung_ist_einstellbar_und_steht_in_beiden_kanaelen(ws, lis, cfg2):
    from stellwerk import bridgelink as bl
    assert bl.ablehnung_skript({"bei_ablehnung": "warten"}, "/x") == ""
    with pytest.raises(bl.BridgeLinkError, match="abweisen"):
        bl.ablehnung_skript({"bei_ablehnung": "wegwerfen"}, "/x")

    def antwort_skripte(xml):
        wurzel = ET.fromstring(xml)
        return {c.findtext("name"): c.findtext("responseTransformer/elements//script") for c in wurzel.iter("connector")
                if c.findtext("metaDataId") != "0"}
    aufgabe(ws, lis)
    skripte_ = antwort_skripte(schnittstelle.felder_kanal(ws, "task000001", cfg2).xml)
    assert "ERROR" in skripte_["FHIR-Ziel"] and skripte_["Prüfablage"] is None and skripte_["Prüfhinweise"] is None
    cfg2["bridgelink"]["kanal"]["bei_ablehnung"] = "warten"
    k = schnittstelle.felder_kanal(ws, "task000001", cfg2)
    assert antwort_skripte(k.xml)["FHIR-Ziel"] is None and any("hält alle folgenden auf" in h for h in k.hinweise)
    cfg2["bridgelink"]["kanal"]["bei_ablehnung"] = "abweisen"
    assert any("nicht aufgehalten" in h for h in schnittstelle.felder_kanal(ws, "task000001", cfg2).hinweise)
    cfg2["bridgelink"]["kanal"]["bei_ablehnung"] = "egal"
    with pytest.raises(ApiError, match="bei_ablehnung"):
        schnittstelle.felder_kanal(ws, "task000001", cfg2)

    from stellwerk import config
    cid, xml = bl.channel_xml({**config.DEFAULTS["bridgelink"]["kanal"], "fhir_ziel": "http://cdr.haus/fhir", "pruefablage": "/ablage"}, "u", "p")
    assert "ERROR" in antwort_skripte(xml)["FHIR-Ziel"]


def test_data_pruner_zustand(monkeypatch):
    from stellwerk import bridgelink as bl
    antworten = {}

    def fake(self, method, path, *a, **k):
        if isinstance(antworten["wert"], Exception):
            raise antworten["wert"]
        return 200, json.dumps({"properties": {"property": [{"@name": "archiveEnabled", "$": "<boolean>false</boolean>"},
                                                            {"@name": "enabled", "$": antworten["wert"]}]}}).encode()
    monkeypatch.setattr(bl.BridgeLink, "login", lambda self: None)
    monkeypatch.setattr(bl.BridgeLink, "_call", fake)
    monkeypatch.setenv("STELLWERK_BRIDGELINK_PASSWORT", "x")
    server = bl.BridgeLink({"api": "https://bl:8443/api", "benutzer": "a"})
    antworten["wert"] = True
    assert server.pruner_aktiv() is True and server.pruner_hinweis() == ""
    antworten["wert"] = False
    assert server.pruner_aktiv() is False and "ausgeschaltet" in server.pruner_hinweis() and "Patientendaten" in server.pruner_hinweis()
    antworten["wert"] = bl.BridgeLinkError("nicht erreichbar")
    assert server.pruner_aktiv() is None and "nicht lesen" in server.pruner_hinweis()


def test_einrichten_meldet_ausgeschalteten_pruner(ws, lis, cfg2, monkeypatch):
    from stellwerk import bridgelink as bl
    aufgabe(ws, lis)
    k = schnittstelle.felder_kanal(ws, "task000001", cfg2)
    cfg2["bridgelink"].update(api="https://bl:8443/api", benutzer="a")
    monkeypatch.setenv("STELLWERK_BRIDGELINK_PASSWORT", "x")
    monkeypatch.setattr(bl.BridgeLink, "install", lambda self, cid, xml: "angelegt")
    monkeypatch.setattr(bl.BridgeLink, "pruner_aktiv", lambda self: False)
    vorher = len(k.hinweise)
    assert schnittstelle.einrichten(k, cfg2) == "angelegt" and "Data Pruner" in k.hinweise[-1] and len(k.hinweise) == vorher + 1
    monkeypatch.setattr(bl.BridgeLink, "pruner_aktiv", lambda self: True)
    k2 = schnittstelle.felder_kanal(ws, "task000001", cfg2)
    schnittstelle.einrichten(k2, cfg2)
    assert not any("Data Pruner" in h for h in k2.hinweise)
