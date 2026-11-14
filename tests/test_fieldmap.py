import json

from fhir.resources.R4B.bundle import Bundle

from stellwerk import fieldmap
from stellwerk.sqlsource import run_query
from stellwerk.workbench import Workspace, handle

Q = "SELECT * FROM PAT_LAB_RESULTS ORDER BY auftrag_nr"
REGELN = {"pat_id": {"ziel": "patient.id"}, "fall_nr": {"ziel": "fall.id"}, "auftrag_nr": {"ziel": "auftrag.id"},
          "geb_datum": {"ziel": "patient.birthDate"}, "geschlecht": {"ziel": "patient.gender"}, "nachname": {"ziel": "patient.family"},
          "vorname": {"ziel": "patient.given"}, "abnahme_zeit": {"ziel": "obs.zeit"},
          "krea_wert": {"ziel": "obs.wert", "loinc": "2160-0"}, "krea_einheit": {"ziel": "obs.einheit", "fuer": "krea_wert"},
          "crp": {"ziel": "obs.wert", "loinc": "1988-5", "einheit": "mg/l"}, "kommentar": {"ziel": "ignorieren"}}


def test_vorschlaege_nur_als_vorschlag(bsp):
    cols, _ = run_query(f"sqlite:///{bsp / 'kis.db'}", Q)
    v = fieldmap.suggest(cols, {}, ["kalium"])
    assert v["pat_id"]["ziel"] == "patient.id" and v["krea_wert"]["loinc"] == "2160-0" and "mg/dl" in v["krea_wert"]["begruendung"]
    assert v["krea_einheit"] == {**v["krea_einheit"], "ziel": "obs.einheit", "fuer": "krea_wert"}
    assert v["hb_wert"]["loinc"] == "718-7" and "kalium" not in v and "kommentar" not in v


def test_werte_unveraendert_und_meldungen(bsp, cfg):
    cols, rows = run_query(f"sqlite:///{bsp / 'kis.db'}", Q)
    bundles, f, labels = fieldmap.build(cols, rows, REGELN, cfg, "final")
    codes = {x.code for x in f}
    assert {"MAP_EINHEITEN", "MAP_LOINC_EINHEIT", "MAP_KEINE_ZAHL", "MAP_OFFEN"} <= codes and "MAP_STATUS" not in codes
    text = fieldmap.dump_json(bundles[0])
    assert '"value": 1.10' in text and '"unit": "mg/dl"' in text                       # Nachkommastellen bleiben
    b = json.loads(text)
    Bundle.model_validate(b)
    obs = [e for e in b["entry"] if e["resource"]["resourceType"] == "Observation"]
    assert obs[0]["request"]["method"] == "PUT" and obs[0]["resource"]["effectiveDateTime"] == "2026-11-06T07:40:00+01:00"
    assert obs[0]["resource"]["encounter"]["identifier"]["value"] == "2026004711"
    drei = json.loads(fieldmap.dump_json(bundles[2]))["entry"]
    krea, crp = drei[1]["resource"], drei[2]["resource"]
    assert krea["valueQuantity"] == {"value": 97, "unit": "µmol/l"}                     # keine Umrechnung
    assert crp["valueString"] == "<0,6 mg/l" and "valueQuantity" not in crp            # kein Zerlegen
    assert labels[2].startswith("Zeile 3 · A26-0003")


def test_ohne_pflichtzuordnung_kein_bundle(bsp, cfg):
    cols, rows = run_query(f"sqlite:///{bsp / 'kis.db'}", Q)
    bundles, f, _ = fieldmap.build(cols, rows, {"krea_wert": {"ziel": "obs.wert", "loinc": "kreatinin"}}, cfg)
    assert bundles == [None] * 4 and {"MAP_PATIENT", "MAP_LOINC"} <= {x.code for x in f}
    _, f, _ = fieldmap.build(cols, rows, {"pat_id": {"ziel": "patient.id"}, "kalium": {"ziel": "obs.wert", "loinc": "2823-3"}}, cfg)
    assert {"MAP_STATUS", "MAP_AUFTRAG", "MAP_ZEIT", "MAP_OHNE_EINHEIT"} <= {x.code for x in f}


def test_workbench_feldmodus(bsp, cfg, tmp_path):
    ws = Workspace(str(tmp_path / "ws"))
    call = lambda m, p, body=None: handle(ws, cfg, m, "/api/" + p, {}, json.dumps(body).encode() if body is not None else b"")[1]   # noqa: E731
    t = {"art": "sql", "modus": "felder", "db": f"sqlite:///{bsp / 'kis.db'}", "abfrage": Q}
    call("PUT", "aufgabe/task000003", t)
    r = call("POST", "aufgabe/task000003/lauf")
    assert r["status"] == "fehler" and r["bundle"] is None
    assert next(c for c in r["spalten"] if c["name"] == "crp")["vorschlag"]["loinc"] == "1988-5"
    call("PUT", "aufgabe/task000003", {**t, "dokument": 2, "felder": {"regeln": REGELN, "status": "final", "abgelehnt": ["hb_wert"]}})
    r = call("POST", "aufgabe/task000003/lauf")
    assert r["status"] == "pruefen" and r["bundles"] == 4 and r["bundle"]["entry"][1]["resource"]["valueQuantity"]["value"] == "@@dez:97@@"
    assert next(c for c in r["spalten"] if c["name"] == "hb_wert")["vorschlag"] is None
    out = call("POST", "aufgabe/task000003/ablegen", {"ordner": str(tmp_path / "ab")})
    files = sorted((tmp_path / "ab").iterdir())
    assert out["datei"].startswith("4 Bundles") and len(files) == 4 and '"value": 1.10' in files[0].read_text("utf-8")
