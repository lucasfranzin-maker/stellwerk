import json

import pytest

from stellwerk import workbench
from stellwerk.workbench import ApiError, Workspace, handle


def call(ws, cfg, method, path, body=None, query=None, raw=False):
    data = body if raw else (json.dumps(body).encode() if body is not None else b"")
    return handle(ws, cfg, method, "/api/" + path, query or {}, data)[1]


def test_baum_speichern_und_pruefen(tmp_path, cfg):
    ws = Workspace(str(tmp_path))
    tree = {"kinder": [{"id": "proj000001", "name": "Haus A", "typ": "ordner", "kinder": [
        {"id": "task000001", "name": "KIS SQL", "typ": "aufgabe", "boese": "x"}]}]}
    out = call(ws, cfg, "PUT", "baum", tree)
    assert out["kinder"][0]["kinder"][0] == {"id": "task000001", "name": "KIS SQL", "typ": "aufgabe"} and "entlassbrief" in out["vorlagen"]
    with pytest.raises(ApiError):
        call(ws, cfg, "PUT", "baum", {"kinder": [{"id": "../../etc", "name": "x", "typ": "ordner"}]})
    with pytest.raises(ApiError):
        call(ws, cfg, "GET", "aufgabe/..%2F..")


def test_aufgabe_lauf_zuordnung_aendern_und_ablegen(tmp_path, cfg, bsp):
    ws = Workspace(str(tmp_path / "ws"))
    with pytest.raises(ApiError, match="Noch keine Quelldatei"):
        call(ws, cfg, "POST", "aufgabe/task000001/lauf")
    call(ws, cfg, "POST", "aufgabe/task000001/quelle", (bsp / "arztbrief_mdm.hl7").read_bytes(), {"name": "a.hl7"}, raw=True)
    r = call(ws, cfg, "POST", "aufgabe/task000001/lauf")
    assert r["status"] == "pruefen" and r["roh"]["text"].startswith("MSH|") and r["bundle"]["type"] == "document"
    rest = next(b for b in r["bloecke"] if b["ueberschrift"] == "Sozialdienst")
    assert rest["abschnitt"] == "weitere" and any(b["code"] == "ABS_UNBEKANNT" for b in r["befunde"])

    call(ws, cfg, "PUT", "aufgabe/task000001", {"aliase": {"Sozialdienst": "empfehlung"}, "meta": {"author": "Dr. Test", "x": "y"}})
    r = call(ws, cfg, "POST", "aufgabe/task000001/lauf")
    assert next(b for b in r["bloecke"] if b["ueberschrift"] == "Sozialdienst")["abschnitt"] == "empfehlung"
    assert not any(b["code"] == "ABS_UNBEKANNT" for b in r["befunde"]) and r["kopf"]["author"] == "Dr. Test"

    out = call(ws, cfg, "POST", "aufgabe/task000001/ablegen", {"ordner": str(tmp_path / "abholen")})
    assert json.loads(open(out["datei"], encoding="utf-8").read())["resourceType"] == "Bundle"
    assert handle(ws, cfg, "GET", "/api/aufgabe/task000001/bundle", {}, b"")[1].startswith(b"{")


def test_sql_aufgabe(tmp_path, cfg, bsp):
    ws = Workspace(str(tmp_path))
    call(ws, cfg, "PUT", "aufgabe/task000002", {"art": "sql", "db": f"sqlite:///{bsp / 'kis.db'}", "abfrage": "SELECT * FROM ARZTBRIEF ORDER BY DOKNR", "dokument": 1})
    r = call(ws, cfg, "POST", "aufgabe/task000002/lauf")
    assert len(r["dokumente"]) == 2 and r["dokument"] == 1 and r["kopf"]["patient_id"] == "4711002" and r["roh"]["gesamt"] == 2
    call(ws, cfg, "PUT", "aufgabe/task000002", {"art": "sql", "db": f"sqlite:///{bsp / 'kis.db'}", "abfrage": "DELETE FROM ARZTBRIEF"})
    with pytest.raises(ApiError, match="nur lesende"):
        call(ws, cfg, "POST", "aufgabe/task000002/lauf")


def test_seite_wird_ausgeliefert():
    from importlib import resources
    html = (resources.files("stellwerk") / "web" / "index.html").read_text("utf-8")
    assert "<title>Stellwerk</title>" in html and "https://" not in html.replace("http://www.w3.org", "") and workbench.VORGABE["art"] == "datei"
