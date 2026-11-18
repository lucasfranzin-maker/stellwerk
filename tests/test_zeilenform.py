"""Laborwerte in Zeilenform (eine Zeile je Ergebnis, Analysecode + Codetabelle): Bau, Übergabepaket, Kanal und Nachbau."""
import io
import json
import shutil
import sqlite3
import subprocess
import zipfile
from pathlib import Path

import pytest

from stellwerk import fieldmap, schnittstelle, uebergabe
from stellwerk.db import Store
from stellwerk.workbench import ApiError, Workspace

NODE = shutil.which("node")
SQL = ("SELECT lfd_nr, auftrag_nr, pat_id, fall_nr, abnahme_zeit, analyt, ergebnis, einheit, nachname, vorname, geb_datum, geschlecht "
       "FROM LABORZEILE")
CODES = {"KREA": {"loinc": "2160-0", "anzeige": "", "einheit": ""}, "K": {"loinc": "2823-3", "anzeige": "Kalium", "einheit": "mmol/l"},
         "CRP": {"loinc": "1988-5", "anzeige": "", "einheit": "mg/l"}}
REGELN = {"lfd_nr": {"ziel": "ignorieren"}, "auftrag_nr": {"ziel": "auftrag.id"}, "pat_id": {"ziel": "patient.id"}, "fall_nr": {"ziel": "fall.id"},
          "abnahme_zeit": {"ziel": "obs.zeit"}, "analyt": {"ziel": "obs.code", "codes": CODES}, "ergebnis": {"ziel": "obs.wert"},
          "einheit": {"ziel": "obs.einheit"}, "nachname": {"ziel": "patient.family"}, "vorname": {"ziel": "patient.given"},
          "geb_datum": {"ziel": "patient.birthDate"}, "geschlecht": {"ziel": "patient.gender"}}
ZEILEN = [
    (1, "A100", "4711", "F1", "12.11.2026 08:15", "KREA", "1,10", "mg/dl", "Muster", "Erika Maria", "12.08.1964", "W"),
    (2, "A100", "4711", "F1", "12.11.2026 08:15", "K", "4,2", "", "Muster", "Erika Maria", "12.08.1964", "W"),
    (3, "A101", "4712", "F2", "13.07.2026 14:05", "CRP", "<0,6", "mg/l", "Probe", "Hans", "01.01.1950", "M"),
    (4, "A101", "4712", "F2", "13.07.2026 14:05", "XYZ", "9", "u", "Probe", "Hans", "01.01.1950", "M"),
    (5, "A102", "4713", "F3", "14.11.2026 09:00", "KREA", "", "mg/dl", "Leer", "Lea", "02.02.1970", "D"),
    (6, "A103", "4714", "F4", "14.11.2026 09:30", "", "5", "mg/l", "Ohne", "Code", "03.03.1980", "W"),
]


@pytest.fixture
def ws(tmp_path):
    db = tmp_path / "lab.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE LABORZEILE (lfd_nr INTEGER, auftrag_nr TEXT, pat_id TEXT, fall_nr TEXT, abnahme_zeit TEXT, analyt TEXT, ergebnis TEXT, "
                "einheit TEXT, nachname TEXT, vorname TEXT, geb_datum TEXT, geschlecht TEXT)")
    con.executemany("INSERT INTO LABORZEILE VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", ZEILEN)
    con.commit()
    con.close()
    store = Store(f"sqlite:///{tmp_path / 'ws.db'}")
    store.migrate()
    w = Workspace(store)
    w.save_tree({"kinder": [{"id": "task000001", "name": "Labor Zeilenform", "typ": "aufgabe"}]})
    w.save_task("task000001", {"art": "sql", "modus": "felder", "db": f"sqlite:///{db}", "abfrage": SQL,
                               "felder": {"regeln": REGELN, "abgelehnt": [], "status": "final"}, "kanal": {"neu_spalte": "lfd_nr"}})
    return w


@pytest.fixture
def cfg2(cfg, tmp_path):
    cfg["bridgelink"]["kanal"].update(fhir_ziel="https://cdr.haus/fhir", pruefablage=str(tmp_path / "p"), stand_ordner=str(tmp_path / "s"))
    cfg["systeme"]["auftrag"] = "https://haus.example/sid/auftrag"
    return cfg


def bauen(cfg, regeln=None, rows=ZEILEN):
    cols = ["lfd_nr", "auftrag_nr", "pat_id", "fall_nr", "abnahme_zeit", "analyt", "ergebnis", "einheit", "nachname", "vorname", "geb_datum", "geschlecht"]
    return fieldmap.build(cols, rows, regeln or REGELN, cfg, "final")


def test_zeile_wird_zu_einer_observation(cfg2):
    bundles, findings, _ = bauen(cfg2)
    obs = [b["entry"][1]["resource"] for b in bundles if b]
    assert [o["code"]["coding"][0]["code"] for o in obs] == ["2160-0", "2823-3", "1988-5"]
    assert [o["code"]["text"] for o in obs] == ["KREA", "K", "CRP"]
    assert obs[0]["valueQuantity"] == {"value": "@@dez:1.10@@", "unit": "mg/dl"}                  # Nachkommastellen bleiben
    assert obs[1]["valueQuantity"]["unit"] == "mmol/l" and obs[1]["code"]["coding"][0]["display"] == "Kalium"     # feste Einheit des Codes
    assert obs[2]["valueString"] == "<0,6 mg/l".replace(",", ",") and "valueQuantity" not in obs[2]
    assert obs[0]["identifier"][0]["value"] == "A100-2160-0" and obs[1]["identifier"][0]["value"] == "A100-2823-3"   # zwei Zeilen, ein Auftrag
    assert bundles[3] is None and bundles[4] is None and bundles[5] is None                       # unbekannter Code, leerer Wert, leerer Code
    codes = {f.code: f for f in findings}
    assert "XYZ" in codes["MAP_CODE_UNBEKANNT"].message and "Observation" in codes["MAP_CODE_UNBEKANNT"].message
    assert codes["MAP_CODE_LEER"].severity == "pruefen" and codes["MAP_LEER"].where == "KREA"


def test_zeilenform_prueft_die_zuordnung(cfg2):
    r = json.loads(json.dumps(REGELN))
    r["analyt"]["codes"] = {}
    assert [f.code for f in bauen(cfg2, r)[1] if f.severity == "fehler"] == ["MAP_CODETABELLE"]
    r = json.loads(json.dumps(REGELN))
    r["analyt"]["codes"]["KREA"]["loinc"] = "kreatinin"
    err = [f for f in bauen(cfg2, r)[1] if f.severity == "fehler"]
    assert err[0].code == "MAP_LOINC" and err[0].where == "KREA"
    r = json.loads(json.dumps(REGELN))
    r["nachname"] = {"ziel": "obs.wert", "loinc": "1-1"}
    assert "MAP_WERT_EINDEUTIG" in [f.code for f in bauen(cfg2, r)[1]]


def test_codetabelle_wird_beim_speichern_bereinigt(ws):
    t = json.loads(json.dumps(ws.task("task000001")))
    t["felder"]["regeln"]["analyt"]["codes"] = {" KREA ": {"loinc": " 2160-0 ", "x": "weg"}, "": {"loinc": "1-1"}, "BAD": "kein Objekt"}
    gespeichert = ws.save_task("task000001", t)["felder"]["regeln"]["analyt"]["codes"]
    assert gespeichert == {"KREA": {"loinc": "2160-0", "anzeige": "", "einheit": ""}}
    t["felder"]["regeln"]["analyt"]["codes"] = {str(i): {"loinc": "1-1"} for i in range(2001)}
    with pytest.raises(ApiError, match="2000"):
        ws.save_task("task000001", t)


def paket(ws, cfg, tmp_path):
    _, data = uebergabe.build_package(ws, "task000001", cfg, True)
    zipfile.ZipFile(io.BytesIO(data)).extractall(tmp_path / "paket")
    return next((tmp_path / "paket").iterdir())


def test_paket_beschreibt_die_zeilenform(ws, cfg2, tmp_path):
    root = paket(ws, cfg2, tmp_path)
    Z = json.loads((root / "zuordnung.json").read_text(encoding="utf-8"))
    kc = next(s for s in Z["spalten"] if s["ziel"] == "obs.code")
    assert [c["code"] for c in kc["codes"]] == ["CRP", "K", "KREA"] and kc["codes"][1]["einheit_fest"] == "mmol/l"
    assert next(s for s in Z["spalten"] if s["ziel"] == "obs.wert")["einheit_spalte"] == "einheit"
    md = (root / "LIESMICH.md").read_text(encoding="utf-8")
    assert "## Codetabelle (3 Analysecodes)" in md and "| `KREA` | 2160-0 |" in md and "Zeilenform: Jede Zeile enthält ein Ergebnis" in md
    faelle = sorted(p.parent.name for p in (root / "testfaelle").glob("*/zeile.json"))
    assert "07-code-nicht-in-der-tabelle" in faelle
    assert json.loads((root / "testfaelle/07-code-nicht-in-der-tabelle/erwartet.json").read_text(encoding="utf-8")) is None
    assert "Je gefüllter Messwert-Spalte" not in md


@pytest.mark.skipif(not NODE, reason="node ist nicht installiert")
def test_kanal_und_nachbau_treffen_jedes_bundle(ws, cfg2, tmp_path):
    root = paket(ws, cfg2, tmp_path)
    tests = Path(__file__).parent
    ist = json.loads(subprocess.run([NODE, str(tests / "kanal_nachbau.js"), str(root)], capture_output=True, text=True, check=True).stdout)
    faelle = sorted(p.parent.relative_to(root).as_posix() for p in root.glob("*/*/zeile.json"))
    assert len(faelle) > 7 and sorted(ist) == faelle
    for f in faelle:
        soll = uebergabe.load_bundle((root / f / "erwartet.json").read_text(encoding="utf-8"))
        assert uebergabe.compare(soll, uebergabe.load_bundle(ist[f])) == [], ("kanal_bauen.js", f)
        z = root / f / "zeile.json"
        nach = subprocess.run([NODE, str(tests / "nachbau.js"), str(root), str(z)], capture_output=True, text=True, check=True).stdout
        assert uebergabe.compare(soll, uebergabe.load_bundle(nach)) == [], ("nachbau.js", f)


@pytest.mark.skipif(not NODE, reason="node ist nicht installiert")
def test_kanal_meldet_unbekannten_code_als_grund(ws, cfg2, tmp_path):
    k = schnittstelle.felder_kanal(ws, "task000001", cfg2)
    assert '"codes"' in k.xml and "2160-0" in k.xml and "Kalium" in k.xml        # die Codetabelle steht im Kanal
    js = ("const B=require(process.argv[1]);const Z=JSON.parse(process.argv[2]);"
          "const env={offset:()=>'+01:00',uuid:()=>'u'};"
          "process.stdout.write(JSON.stringify([B.bauen({pat_id:'1',analyt:'NIX',ergebnis:'5'},Z,env),B.bauen({pat_id:'1',analyt:'',ergebnis:'5'},Z,env)].map(r=>[r.grund,r.hinweise.map(h=>h.code)])))")
    Z = {"ziel": {"namensraeume": {"patient": "p", "fall": "f", "auftrag": "a"}, "zeitzone": "Europe/Berlin", "observation_status": "final"}, "geschlecht_codes": {},
         "spalten": [{"spalte": "pat_id", "stand": "zugeordnet", "ziel": "patient.id"}, {"spalte": "ergebnis", "stand": "zugeordnet", "ziel": "obs.wert"},
                     {"spalte": "analyt", "stand": "zugeordnet", "ziel": "obs.code", "codes": [{"code": "K", "loinc": "2823-3"}]}]}
    out = json.loads(subprocess.run([NODE, "-e", js, str(Path(__file__).parent.parent / "src/stellwerk/kanal_bauen.js"), json.dumps(Z)],
                                    capture_output=True, text=True, check=True).stdout)
    assert out == [["CODE_UNBEKANNT", ["CODE_UNBEKANNT"]], ["CODE_LEER", ["CODE_LEER"]]]
