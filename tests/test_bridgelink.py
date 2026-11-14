import json
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from stellwerk import bridgelink
from stellwerk.pipeline import ingest, process, deliver


class Empfang(BaseHTTPRequestHandler):
    """Steht für den BridgeLink-Kanal: nimmt POSTs an, merkt sich Kopfzeilen und Inhalt."""
    seen: list = []
    code = 200

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Empfang.seen.append((self.path, dict(self.headers), body))
        self.send_response(Empfang.code)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def empfang():
    srv = HTTPServer(("127.0.0.1", 0), Empfang)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    Empfang.seen, Empfang.code = [], 200
    yield f"http://127.0.0.1:{srv.server_port}/stellwerk"
    srv.shutdown()


def test_kanal_xml(monkeypatch):
    monkeypatch.setenv("FHIR_PW", "cdr-geheim")
    k = {"name": "Stellwerk FHIR-Eingang", "port": 8095, "pfad": "/stellwerk", "fhir_ziel": "https://cdr.haus/fhir",
         "fhir_auth": "basic", "fhir_benutzer": "bl", "fhir_passwort_env": "FHIR_PW"}
    cid, xml = bridgelink.channel_xml(k, "stellwerk", "p&w<1", "26.9.0")
    root = ET.fromstring(xml)
    assert root.tag == "channel" and root.get("version") == "26.9.0" and root.findtext("id") == cid
    assert cid == bridgelink.channel_xml(k, "x", "y")[0]                      # gleiche ID bei erneutem Einrichten
    src = root.find("sourceConnector/properties")
    assert src.findtext("contextPath") == "/stellwerk" and src.findtext("listenerConnectorProperties/port") == "8095"
    assert src.find(".//credentials/entry").findall("string")[1].text == "p&w<1"
    dests = root.findall("destinationConnectors/connector")
    assert [d.findtext("name") for d in dests] == ["FHIR-Ziel", "Prüfablage"]
    assert dests[0].findtext("properties/host") == "https://cdr.haus/fhir" and dests[0].findtext("properties/password") == "cdr-geheim"
    assert dests[0].findtext("properties/destinationConnectorProperties/queueEnabled") == "true"
    assert "stellwerkStatus') == 'ok'" in dests[0].findtext(".//JavaScriptRule/script".replace("JavaScriptRule", "com.mirth.connect.plugins.javascriptrule.JavaScriptRule"))
    assert root.findtext("properties/encryptData") == "true" and "{v}" not in xml
    _, nur_ablage = bridgelink.channel_xml({"name": "X"}, "a", "b")
    assert [d.findtext("name") for d in ET.fromstring(nur_ablage).findall("destinationConnectors/connector")] == ["Ablage"]
    with pytest.raises(bridgelink.BridgeLinkError):
        bridgelink.channel_xml(k, "a", "")


def test_routing_merkmale_ohne_patientendaten(bsp, cfg):
    res = process(ingest((bsp / "arztbrief_mdm.hl7").read_bytes(), "a.hl7", cfg)[0], cfg)
    h = bridgelink.routing_headers(res.bundle, res.status, "ORBIS", "abc")
    assert h == {"X-Stellwerk-Bundle-Typ": "document", "X-Stellwerk-Status": "pruefen", "X-Stellwerk-Inhalt": "Composition",
                 "X-Request-ID": "abc", "X-Stellwerk-Dokumenttyp": "AD010103", "X-Stellwerk-Quelle": "ORBIS"}
    assert "Mustermann" not in json.dumps(h)


def test_zustellung_mit_anmeldung(bsp, cfg, empfang, monkeypatch):
    monkeypatch.setenv("STELLWERK_ZIEL_PASSWORT", "geheim")
    cfg["zustellung"].update(ziel=empfang, benutzer="stellwerk", bei_pruefen="zustellen")
    res = process(ingest((bsp / "arztbrief_mdm.hl7").read_bytes(), "a.hl7", cfg)[0], cfg)
    deliver(res, cfg)
    path, headers, body = Empfang.seen[0]
    assert res.delivered and path == "/stellwerk" and headers["Authorization"] == bridgelink.basic_auth("stellwerk", "geheim")
    assert headers["X-Stellwerk-Status"] == "pruefen" and headers["X-Stellwerk-Quelle"] == "ORBIS"
    assert json.loads(body)["type"] == "document"
    Empfang.code = 500
    res = process(ingest((bsp / "arztbrief_mdm.hl7").read_bytes(), "a.hl7", cfg)[0], cfg)
    deliver(res, cfg)
    assert not res.delivered and any(f.code == "ZUSTELLUNG" for f in res.findings)


def test_workbench_senden(api, cfg, bsp, empfang, monkeypatch):
    from stellwerk.workbench import ApiError
    monkeypatch.setenv("STELLWERK_ZIEL_PASSWORT", "geheim")
    cfg["zustellung"].update(ziel=empfang, benutzer="stellwerk")
    assert api("GET", "zustellung")["ziel"] == empfang
    api("POST", "aufgabe/task000005/quelle", (bsp / "arztbrief_mdm.hl7").read_bytes(), {"name": "a.hl7"}, raw=True)
    with pytest.raises(ApiError, match="Kein zustellbares"):
        api("POST", "aufgabe/task000005/senden")
    api("POST", "aufgabe/task000005/lauf")
    out = api("POST", "aufgabe/task000005/senden")
    assert out == {"ziel": empfang, "bundles": 1, "status": "pruefen"} and Empfang.seen[-1][1]["X-Stellwerk-Quelle"] == "Datei"
    Empfang.code = 503
    with pytest.raises(ApiError, match="HTTP 503"):
        api("POST", "aufgabe/task000005/senden")
    assert api("GET", "audit")["eintraege"][0]["aktion"] == "bundle.gesendet"


def _stat(**n):
    return {"@class": "linked-hash-map", "entry": [{"com.mirth.connect.donkey.model.message.Status": k, "long": v} for k, v in n.items()]}


def test_status_aus_dashboard(monkeypatch):
    monkeypatch.setenv("STELLWERK_BRIDGELINK_PASSWORT", "pw")
    """Antwortform wie von BridgeLink 26.9.0 geliefert (GET /channels/{id}/status)."""
    antwort = {"dashboardStatus": {"name": "Stellwerk FHIR-Eingang", "state": "STARTED", "statistics": _stat(RECEIVED=5, FILTERED=5, SENT=4, ERROR=0),
               "childStatuses": {"dashboardStatus": [
                   {"name": "Source", "metaDataId": 0, "queued": 0, "statistics": _stat(RECEIVED=5, FILTERED=1, SENT=0, ERROR=0)},
                   {"name": "FHIR-Ziel", "metaDataId": 1, "state": "STARTED", "queued": 1, "statistics": _stat(RECEIVED=3, FILTERED=1, SENT=2, ERROR=0)},
                   {"name": "Prüfablage", "metaDataId": 2, "state": "STARTED", "queued": 0, "statistics": _stat(RECEIVED=4, FILTERED=2, SENT=2, ERROR=0)}]}}}
    b = bridgelink.BridgeLink({"api": "https://bl:8443/api", "benutzer": "admin"})
    monkeypatch.setattr(b, "login", lambda: None)
    monkeypatch.setattr(b, "_call", lambda *a, **k: (200, json.dumps(antwort).encode()))
    st = b.status("x")
    assert (st["empfangen"], st["abgewiesen"], st["gesendet"], st["warteschlange"], st["fehler"]) == (5, 1, 4, 1, 0)
    assert [z["name"] for z in st["ziele"]] == ["FHIR-Ziel", "Prüfablage"]
