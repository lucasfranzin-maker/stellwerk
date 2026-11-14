import io
import json
import sqlite3
import zipfile

import pytest
from fhir.resources.R4B.bundle import Bundle

from stellwerk import discovery, uebergabe
from stellwerk.cli import main
from stellwerk.sqlsource import run_query

REGELN = {"pat_id": {"ziel": "patient.id"}, "fall_nr": {"ziel": "fall.id"}, "auftrag_nr": {"ziel": "auftrag.id"},
          "geb_datum": {"ziel": "patient.birthDate"}, "geschlecht": {"ziel": "patient.gender"}, "nachname": {"ziel": "patient.family"},
          "vorname": {"ziel": "patient.given"}, "abnahme_zeit": {"ziel": "obs.zeit"},
          "krea_wert": {"ziel": "obs.wert", "loinc": "2160-0"}, "krea_einheit": {"ziel": "obs.einheit", "fuer": "krea_wert"},
          "crp": {"ziel": "obs.wert", "loinc": "1988-5", "einheit": "mg/l"}, "bemerkung": {"ziel": "ignorieren"}, "fallart": {"ziel": "ignorieren"}}
BAUM = {"kinder": [{"id": "ordner01", "name": "Haus", "typ": "ordner", "kinder": [
    {"id": "task000001", "name": "Laborwerte LIS", "typ": "aufgabe"}, {"id": "task000002", "name": "Arztbriefe", "typ": "aufgabe"}]}]}


def _abfragen(db):
    res = discovery.analyse(discovery.crawl(db))
    ab = discovery.propose_queries(res)
    discovery.verify_queries(db, res, ab)
    return ab


def test_abfrage_mit_verknuepfung_zu_fall_und_patient(bsp):
    db = f"sqlite:///{bsp / 'lis.db'}"
    (a,) = _abfragen(db)
    assert a["tabelle"] == "LABORBEFUND" and a["modus"] == "felder" and a["zeilen"] == 5 and "_teile" not in a
    assert "LEFT JOIN FALL f ON f.fall_nr = l.fall_nr" in a["sql"] and "LEFT JOIN PATIENT p ON p.pat_id = f.pat_id" in a["sql"]
    assert "VERMUTET" in a["sql"] and "erklärter Fremdschlüssel" in a["sql"]
    assert [v["art"] for v in a["verknuepfungen"]] == ["vermutet", "Fremdschlüssel"] and a["pruefen"]
    assert a["hinweise"][0].startswith("1 von 5 Zeilen finden keinen Eintrag in FALL")          # der Befund ohne Fall bleibt in der Ausgabe
    cols, rows = run_query(db, a["sql"])
    assert len(rows) == 5 and {"pat_id", "nachname", "geb_datum", "krea_wert", "auftrag_nr"} <= set(cols) and len(cols) == len(set(cols))
    assert [r[cols.index("pat_id")] for r in rows].count(None) == 1


def test_abfrage_verknuepft_keine_bewegungsdaten_miteinander(bsp):
    ab = _abfragen(f"sqlite:///{bsp / 'kis.db'}")
    assert [(a["tabelle"], a["modus"], a["pruefen"], a["verknuepfungen"]) for a in ab] == [
        ("ARZTBRIEF", "dokument", False, []), ("PAT_LAB_RESULTS", "felder", False, [])]
    assert "JOIN" not in ab[0]["sql"] + ab[1]["sql"] and ab[1]["zeilen"] == 4


def test_abfrage_meldet_vervielfachte_zeilen(tmp_path):
    con = sqlite3.connect(tmp_path / "x.db")
    con.execute("CREATE TABLE STAMM (pat_id TEXT, nachname TEXT, geb_datum TEXT)")
    con.execute('CREATE TABLE "LABOR WERTE" (pat_id TEXT, "order" TEXT, krea_wert TEXT)')
    con.executemany("INSERT INTO STAMM VALUES (?,?,?)", [("1", "A", "01.01.1970"), ("1", "A alt", "01.01.1970"), ("2", "B", "02.02.1980")])
    con.executemany('INSERT INTO "LABOR WERTE" VALUES (?,?,?)', [("1", "x", "1,0"), ("2", "y", "0,9")])
    con.commit()
    con.close()
    (a,) = _abfragen(f"sqlite:///{tmp_path / 'x.db'}")
    assert 'FROM "LABOR WERTE" l' in a["sql"] and 'l."order"' in a["sql"]                       # Bezeichner werden geschützt
    assert a["zeilen"] == 3 and a["pruefen"] and a["hinweise"][0].startswith("Die Abfrage liefert 3 Zeilen, LABOR WERTE hat 2")
    assert any("nicht eindeutig" in h for h in a["hinweise"])


def _zip(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    root = z.namelist()[0].split("/")[0]
    return {n[len(root) + 1:]: z.read(n).decode("utf-8", "replace") for n in z.namelist()}, root


@pytest.fixture
def labor(api, bsp):
    db = f"sqlite:///{bsp / 'lis.db'}"
    api("PUT", "baum", BAUM)
    sql = api("POST", "erschliessen", {"db": db})["abfragen"][0]["sql"]
    api("PUT", "aufgabe/task000001", {"art": "sql", "modus": "felder", "db": db, "abfrage": sql,
                                      "felder": {"regeln": REGELN, "abgelehnt": [], "status": "final"}})
    return api


def test_paket_fuer_messwerte_ohne_patientendaten(labor, client):
    r = client.get("/api/aufgabe/task000001/uebergabe")
    assert r.headers["content-disposition"] == 'attachment; filename="uebergabe-Laborwerte_LIS.zip"'
    f, root = _zip(r.content)
    assert root == "uebergabe-Laborwerte_LIS" and {"LIESMICH.md", "abfrage.sql", "zuordnung.csv", "zuordnung.json"} <= set(f)
    assert sorted(n.split("/")[1] for n in f if n.endswith("zeile.json")) == [
        "01-vollstaendig", "02-leere-angaben", "03-kein-zahlenwert", "04-unbekannte-codes", "05-ohne-patientennummer", "06-ohne-messwert"]
    alles = "\n".join(f.values())
    for echt in ("Mustermann", "4711001", "L26-0001", "2026004711", "12.08.1964", "Kontrolle", "148,0"):
        assert echt not in alles, echt                                                         # nichts aus den echten Zeilen
    assert "LEFT JOIN FALL" in f["abfrage.sql"] and f["abfrage.sql"].rstrip().endswith(";")
    z = json.loads(f["zuordnung.json"])
    assert z["status"] == "pruefen" and z["ziel"]["namensraeume"]["patient"] == "https://haus.example/sid/pat"
    stand = {s["spalte"]: s["stand"] for s in z["spalten"]}
    assert stand["krea_wert"] == "zugeordnet" and stand["bemerkung"] == "nicht übernehmen" and stand["kalium"] == "Vorschlag, nicht angenommen"
    assert f["zuordnung.csv"].startswith("﻿Spalte;Stand;FHIR-Pfad") and "kalium;Vorschlag, nicht angenommen" in f["zuordnung.csv"]

    def fall(name):
        return json.loads(f[f"testfaelle/{name}/zeile.json"]), f[f"testfaelle/{name}/erwartet.json"]
    zeile, text = fall("01-vollstaendig")
    assert zeile["krea_wert"] == "1,10" and zeile["geb_datum"] == "03.02.1970" and '"value": 1.10' in text      # Schreibweise der Quelle, Nachkommastellen
    b = json.loads(text)
    Bundle.model_validate(b)
    assert len(b["entry"]) == 3 and "2823-3" not in text                                       # der Vorschlag für kalium ist nicht enthalten
    assert b["entry"][1]["resource"]["effectiveDateTime"] == "2026-01-15T08:30:00+01:00"
    zeile, text = fall("02-leere-angaben")
    b = json.loads(text)
    obs = b["entry"][1]["resource"]
    assert len(b["entry"]) == 2 and obs["valueQuantity"] == {"value": 97} and "encounter" not in obs and "effectiveDateTime" not in obs
    assert set(b["entry"][0]["resource"]) == {"resourceType", "identifier", "name"} and zeile["vorname"] is None
    b = json.loads(fall("03-kein-zahlenwert")[1])
    assert b["entry"][1]["resource"]["valueString"] == "<0,6 mg/dl" and b["entry"][1]["resource"]["effectiveDateTime"].endswith("+02:00")
    pat = json.loads(fall("04-unbekannte-codes")[1])["entry"][0]["resource"]
    assert pat["gender"] == "unknown" and "name" not in pat
    assert fall("05-ohne-patientennummer")[1].strip() == "null" and fall("06-ohne-messwert")[1].strip() == "null"
    lies = f["LIESMICH.md"]
    assert "`1,10` wird `1.10`" in lies and "`15.07.2026 14:05` wird `2026-07-15T14:05:00+02:00`" in lies and "TT.MM.JJJJ HH:MM" in lies
    assert "keine reine Zahl. Sie stehen" in lies and "z. B. „" not in lies                    # Beispielwerte aus der Quelle bleiben draußen
    assert "enthält keine Zeilen" in lies
    assert labor("GET", "audit")["eintraege"][0] == {**labor("GET", "audit")["eintraege"][0], "aktion": "uebergabe.erzeugt", "detail": "ohne Patientendaten"}


def test_paket_mit_echten_zeilen_nur_auf_wunsch(labor, client):
    r = client.get("/api/aufgabe/task000001/uebergabe", params={"daten": 1})
    assert "mit-patientendaten.zip" in r.headers["content-disposition"]
    f, _ = _zip(r.content)
    echte = sorted(n for n in f if n.startswith("echte-zeilen/") and n.endswith("zeile.json"))
    assert len(echte) == 5 and json.loads(f[echte[0]])["nachname"] == "Mustermann"
    assert f["echte-zeilen/zeile-005/erwartet.json"].strip() == "null"                          # Befund ohne Fall: kein Patient, kein Bundle
    assert "**Enthält Patientendaten.**" in f["LIESMICH.md"] and "z. B. „<0,6“" in f["LIESMICH.md"]
    assert labor("GET", "audit")["eintraege"][0]["detail"] == "mit echten Zeilen (Patientendaten)"


def test_paket_erst_nach_behobenen_fehlern(labor):
    from stellwerk.workbench import ApiError
    t = labor("GET", "aufgabe/task000001")
    del t["felder"]["regeln"]["pat_id"]
    labor("PUT", "aufgabe/task000001", t)
    with pytest.raises(ApiError, match="noch Fehler") as e:
        labor("GET", "aufgabe/task000001/uebergabe")
    assert e.value.code == 409


def test_paket_fuer_dokumente(api, client, bsp):
    from stellwerk.workbench import ApiError
    api("PUT", "baum", BAUM)
    with pytest.raises(ApiError, match="keine Quelldatei"):
        api("GET", "aufgabe/task000002/uebergabe")
    api("POST", "aufgabe/task000002/quelle", (bsp / "arztbrief_mdm.hl7").read_bytes(), {"name": "a.hl7"}, raw=True)
    api("PUT", "aufgabe/task000002", {"art": "datei", "aliase": {"Procedere neu": "empfehlung"}})
    api("POST", "aufgabe/task000002/lauf")
    f, _ = _zip(client.get("/api/aufgabe/task000002/uebergabe").content)
    assert set(f) == {"LIESMICH.md", "vorlage.yaml"} and "Procedere neu" in f["vorlage.yaml"] and "/v1/convert" in f["LIESMICH.md"]
    assert "Mustermann" not in "\n".join(f.values()) and "Letzter Lauf in der Workbench: pruefen" in f["LIESMICH.md"]
    f, _ = _zip(client.get("/api/aufgabe/task000002/uebergabe", params={"daten": 1}).content)
    assert {"quelle/a.hl7", "bundle.json"} <= set(f) and "Mustermann" in f["bundle.json"]


def test_vergleich(tmp_path):
    soll = uebergabe.load_bundle('''{"resourceType":"Bundle","type":"transaction","entry":[
      {"fullUrl":"urn:uuid:a","resource":{"resourceType":"Patient","identifier":[{"system":"s","value":"1"}]},
       "request":{"method":"POST","url":"Patient","ifNoneExist":"identifier=s%7C1"}},
      {"fullUrl":"urn:uuid:b","resource":{"resourceType":"Observation","identifier":[{"system":"a","value":"T-1"}],"subject":{"reference":"urn:uuid:a"},
       "effectiveDateTime":"2026-01-15T08:30:00+01:00","valueQuantity":{"value":1.10,"unit":"mg/dl"}},"request":{"method":"PUT","url":"Observation?identifier=a%7CT-1"}}]}''')
    gut = uebergabe.load_bundle('''{"resourceType":"Bundle","type":"transaction","entry":[
      {"fullUrl":"urn:uuid:zwei","resource":{"resourceType":"Observation","identifier":[{"system":"a","value":"T-1"}],"subject":{"reference":"urn:uuid:eins"},
       "effectiveDateTime":"2026-01-15T07:30:00Z","valueQuantity":{"value":1.10,"unit":"mg/dl"}},"request":{"method":"PUT","url":"Observation?identifier=a|T-1"}},
      {"fullUrl":"urn:uuid:eins","resource":{"resourceType":"Patient","identifier":[{"system":"s","value":"1"}]},
       "request":{"method":"POST","url":"Patient","ifNoneExist":"identifier=s|1"}}]}''')
    assert uebergabe.compare(soll, gut) == []                                  # andere Reihenfolge, eigene fullUrl, UTC, unkodierte URL
    schlecht = json.loads(json.dumps(gut, default=str), parse_float=None)
    schlecht["entry"][0]["resource"]["valueQuantity"]["value"] = uebergabe.load_bundle("1.1")
    schlecht["entry"][0]["resource"]["subject"]["reference"] = "urn:uuid:woanders"
    schlecht["entry"][0]["resource"]["effectiveDateTime"] = "2026-01-15T08:30:00Z"
    u = uebergabe.compare(soll, schlecht)
    assert len(u) == 3 and any("andere Nachkommastellen" in x for x in u) and any("subject.reference" in x for x in u) and any("effectiveDateTime" in x for x in u)
    del gut["entry"][0]
    assert uebergabe.compare(soll, gut) == ["Observation T-1: fehlt im gelieferten Bundle"]
    assert uebergabe.compare(None, None) == [] and uebergabe.compare(None, gut) and uebergabe.compare(soll, None)
    a, b = tmp_path / "soll.json", tmp_path / "ist.json"
    a.write_text('{"resourceType":"Bundle","type":"transaction","entry":[]}')
    b.write_text('{"resourceType":"Bundle","type":"batch","entry":[]}')
    assert main(["vergleichen", str(a), str(a)]) == 0 and main(["vergleichen", str(a), str(b)]) == 1


def test_kommandozeile(bsp, tmp_path, capsys, monkeypatch):
    assert main(["discover", f"sqlite:///{bsp / 'lis.db'}", "--abfragen"]) == 1               # 1 = es gibt etwas zu prüfen
    aus = capsys.readouterr().out
    assert "-- ZU PRÜFEN: Messwerte aus LABORBEFUND (5 Zeilen)" in aus and aus.rstrip().endswith("LEFT JOIN PATIENT p ON p.pat_id = f.pat_id;")
    monkeypatch.setenv("STELLWERK_DATENBANK", f"sqlite:///{tmp_path / 's.db'}")
    assert main(["uebergabe"]) == 0 and main(["uebergabe", "task000009"]) == 2


def test_paket_reicht_zum_nachbau(labor, client, tmp_path):
    """Ein Skript, das nur LIESMICH.md und zuordnung.json kennt (tests/nachbau.js), muss jedes erwartete Bundle treffen.

    Ändert sich fieldmap.build, ohne dass die Regeln im Paket mitziehen, schlägt dieser Test an."""
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which("node")
    if not node:
        pytest.skip("node ist nicht installiert")
    z = zipfile.ZipFile(io.BytesIO(client.get("/api/aufgabe/task000001/uebergabe", params={"daten": 1}).content))
    z.extractall(tmp_path)
    root = next(tmp_path.iterdir())
    faelle = sorted(root.glob("testfaelle/*")) + sorted(root.glob("echte-zeilen/*"))
    assert len(faelle) == 11
    for d in faelle:
        ist = subprocess.run([node, str(Path(__file__).parent / "nachbau.js"), str(root), str(d / "zeile.json")],
                             capture_output=True, text=True, check=True).stdout
        soll = uebergabe.load_bundle((d / "erwartet.json").read_text(encoding="utf-8"))
        assert uebergabe.compare(soll, uebergabe.load_bundle(ist)) == [], d.name
