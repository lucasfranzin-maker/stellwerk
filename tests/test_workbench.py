import json

import pytest

from stellwerk.workbench import ApiError


def test_baum_speichern_und_pruefen(api):
    tree = {"kinder": [{"id": "proj000001", "name": "Haus A", "typ": "ordner", "kinder": [
        {"id": "task000001", "name": "KIS SQL", "typ": "aufgabe", "boese": "x"}]}]}
    out = api("PUT", "baum", tree)
    assert out["kinder"][0]["kinder"][0] == {"id": "task000001", "name": "KIS SQL", "typ": "aufgabe"} and "entlassbrief" in out["vorlagen"]
    assert api("GET", "baum")["kinder"][0]["name"] == "Haus A"
    with pytest.raises(ApiError):
        api("PUT", "baum", {"kinder": [{"id": "../../etc", "name": "x", "typ": "ordner"}]})
    with pytest.raises(ApiError):
        api("PUT", "baum", {"kinder": [{"id": "proj000001", "name": "a", "typ": "ordner"}, {"id": "proj000001", "name": "b", "typ": "ordner"}]})
    with pytest.raises(ApiError):
        api("GET", "aufgabe/GROSS-BUCHSTABEN")


def test_aufgabe_loeschen_entfernt_daten(api, bsp):
    api("PUT", "baum", {"kinder": [{"id": "task000009", "name": "x", "typ": "aufgabe"}]})
    api("POST", "aufgabe/task000009/quelle", (bsp / "arztbrief_mdm.hl7").read_bytes(), {"name": "a.hl7"}, raw=True)
    assert api("GET", "aufgabe/task000009")["quelle"] == "a.hl7"
    api("PUT", "baum", {"kinder": []})
    assert api("GET", "aufgabe/task000009")["quelle"] == ""


def test_aufgabe_lauf_zuordnung_aendern_und_ablegen(api, tmp_path, bsp):
    with pytest.raises(ApiError, match="Noch keine Quelldatei"):
        api("POST", "aufgabe/task000001/lauf")
    api("POST", "aufgabe/task000001/quelle", (bsp / "arztbrief_mdm.hl7").read_bytes(), {"name": "a.hl7"}, raw=True)
    r = api("POST", "aufgabe/task000001/lauf")
    assert r["status"] == "pruefen" and r["roh"]["text"].startswith("MSH|") and r["bundle"]["type"] == "document"
    rest = next(b for b in r["bloecke"] if b["ueberschrift"] == "Sozialdienst")
    assert rest["abschnitt"] == "weitere" and any(b["code"] == "ABS_UNBEKANNT" for b in r["befunde"])

    api("PUT", "aufgabe/task000001", {"aliase": {"Sozialdienst": "empfehlung"}, "meta": {"author": "Dr. Test", "x": "y"}})
    r = api("POST", "aufgabe/task000001/lauf")
    assert next(b for b in r["bloecke"] if b["ueberschrift"] == "Sozialdienst")["abschnitt"] == "empfehlung"
    assert not any(b["code"] == "ABS_UNBEKANNT" for b in r["befunde"]) and r["kopf"]["author"] == "Dr. Test"

    out = api("POST", "aufgabe/task000001/ablegen", {"ordner": str(tmp_path / "abholen")})
    assert json.loads(open(out["datei"], encoding="utf-8").read())["resourceType"] == "Bundle"
    assert api("GET", "aufgabe/task000001/bundle").startswith(b"{")

    laeufe = api("GET", "laeufe")["laeufe"]
    assert len(laeufe) == 2 and laeufe[0]["kanal"] == "workbench" and "ABS_UNBEKANNT" in laeufe[1]["codes"]
    assert "Mustermann" not in json.dumps(laeufe) and "a.hl7" not in json.dumps(laeufe)   # keine Patientendaten, keine Dateinamen
    aktionen = [a["aktion"] for a in api("GET", "audit")["eintraege"]]
    assert aktionen[:3] == ["bundle.abgelegt", "aufgabe.gespeichert", "quelle.hochgeladen"]


def test_sql_aufgabe(api, bsp):
    api("PUT", "aufgabe/task000002", {"art": "sql", "db": f"sqlite:///{bsp / 'kis.db'}", "abfrage": "SELECT * FROM ARZTBRIEF ORDER BY DOKNR", "dokument": 1})
    r = api("POST", "aufgabe/task000002/lauf")
    assert len(r["dokumente"]) == 2 and r["dokument"] == 1 and r["kopf"]["patient_id"] == "4711002" and r["roh"]["gesamt"] == 2
    api("PUT", "aufgabe/task000002", {"art": "sql", "db": f"sqlite:///{bsp / 'kis.db'}", "abfrage": "DELETE FROM ARZTBRIEF"})
    with pytest.raises(ApiError, match="nur lesende"):
        api("POST", "aufgabe/task000002/lauf")


def test_seite_header_und_betrieb(client):
    r = client.get("/")
    assert r.status_code == 200 and "<title>Stellwerk</title>" in r.text
    assert "https://" not in r.text.replace("http://www.w3.org", "")
    h = r.headers
    assert "frame-ancestors 'none'" in h["content-security-policy"] and h["x-content-type-options"] == "nosniff"
    assert h["cache-control"] == "no-store"
    assert client.get("/fonts/ibm-plex-mono-latin-400-normal.woff2").headers["content-type"] == "font/woff2"
    assert client.get("/fonts/..%2F..%2Fapp.py").status_code == 404
    assert client.get("/healthz").json() == {"status": "ok"} and client.get("/readyz").json()["datenbank"] == "sqlite"
    assert client.get("/docs").status_code == 404
    r = client.post("/api/aufgabe/task000001/quelle", content=b"x", headers={"content-length": str(60 * 1024 * 1024)})
    assert r.status_code == 413
    assert client.put("/api/baum", content=b"{kaputt").json()["fehler"] == "Kein gültiges JSON."


def test_altbestand_wird_uebernommen(cfg, tmp_path, bsp):
    from fastapi.testclient import TestClient

    from stellwerk.app import create_app
    alt = tmp_path / "alt"
    (alt / "aufgaben" / "task000007").mkdir(parents=True)
    (alt / "baum.json").write_text(json.dumps({"kinder": [{"id": "task000007", "name": "Alt", "typ": "aufgabe"}]}), "utf-8")
    (alt / "aufgaben" / "task000007" / "aufgabe.json").write_text(json.dumps({"art": "datei", "aliase": {"X": "verlauf"}}), "utf-8")
    (alt / "aufgaben" / "task000007" / "quelle.bin").write_bytes((bsp / "arztbrief_mdm.hl7").read_bytes())
    (alt / "aufgaben" / "task000007" / "quelle.name").write_text("a.hl7", "utf-8")
    cfg.update(arbeitsbereich=str(alt), datenbank=f"sqlite:///{alt / 'stellwerk.db'}", ausgabe=str(tmp_path / "out"))
    with TestClient(create_app(cfg)) as c:
        assert c.get("/api/baum").json()["kinder"][0]["name"] == "Alt"
        t = c.get("/api/aufgabe/task000007").json()
        assert t["quelle"] == "a.hl7" and t["aliase"] == {"X": "verlauf"}
        assert c.post("/api/aufgabe/task000007/lauf").json()["status"] == "pruefen"


def test_kein_passwort_in_der_datenbank(api, client, bsp, monkeypatch):
    with pytest.raises(ApiError, match="Platzhalter"):
        api("PUT", "aufgabe/task000004", {"art": "sql", "db": "postgresql://kis:geheim@db.haus/kis", "abfrage": "SELECT 1"})
    monkeypatch.setenv("STELLWERK_DB_KIS", str(bsp / "kis.db"))
    api("PUT", "aufgabe/task000004", {"art": "sql", "db": "sqlite:///${STELLWERK_DB_KIS}", "abfrage": "SELECT * FROM ARZTBRIEF"})
    assert api("POST", "aufgabe/task000004/lauf")["roh"]["gesamt"] == 2
    api("POST", "erschliessen", {"db": "sqlite:///${STELLWERK_DB_KIS}", "entscheidungen": {"ARZTBRIEF.ARZT": "autor"}})
    with pytest.raises(ApiError):
        api("POST", "erschliessen", {"db": "postgresql://kis:geheim@127.0.0.1:1/kis", "entscheidungen": {"A.B": "autor"}})
    store = client.app.state.store
    assert store.decisions("sqlite:///${STELLWERK_DB_KIS}") == {"ARZTBRIEF.ARZT": "autor"}
    assert store.decisions("postgresql://kis:***@127.0.0.1:1/kis") == {"A.B": "autor"}
    assert "geheim" not in str(store.audit_log()) and "geheim" not in str(store.decisions("postgresql://kis:geheim@127.0.0.1:1/kis"))


def test_fehlender_datenbanktreiber_ist_eine_meldung(api, monkeypatch):
    """Ist das Python-Modul für eine Datenbankart nicht installiert, sagt die Meldung, was nachzuinstallieren ist.
    Vorher war das ein Serverfehler ohne Hinweis (aufgefallen in der CI, wo psycopg fehlt)."""
    import sqlalchemy

    from stellwerk import sqlsource
    from stellwerk.db import Store

    def kein_treiber(*a, **k):
        raise ModuleNotFoundError("No module named 'oracledb'", name="oracledb")
    monkeypatch.setattr(sqlalchemy, "create_engine", kein_treiber)
    monkeypatch.setenv("STELLWERK_DB_ORA", "geheim")
    with pytest.raises(ApiError, match="oracledb.*nicht installiert.*pip install oracledb"):
        api("POST", "erschliessen", {"db": "oracle+oracledb://kis:${STELLWERK_DB_ORA}@db.haus/kis"})
    with pytest.raises(sqlsource.SqlError, match="oracledb.*nicht installiert"):
        sqlsource.run_query("oracle+oracledb://kis:${STELLWERK_DB_ORA}@db.haus/kis", "SELECT 1 FROM dual")
    monkeypatch.setattr("stellwerk.db.create_engine", kein_treiber)
    with pytest.raises(sqlsource.SqlError, match="Datenbank von Stellwerk"):
        Store("oracle+oracledb://stellwerk@db.haus/stellwerk")
