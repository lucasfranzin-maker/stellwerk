import json
import sqlite3

import pytest

from stellwerk import discovery
from stellwerk.llm import LLMError, LocalLLM


def test_schema_bedeutung_und_zusammenhaenge(bsp):
    res = discovery.analyse(discovery.crawl(f"sqlite:///{bsp / 'kis.db'}"))
    tab = {t["name"]: t for t in res["tabellen"]}
    lab = {c["name"]: c for c in tab["PAT_LAB_RESULTS"]["spalten"]}
    assert tab["PAT_LAB_RESULTS"]["klasse"] == "Messwerte" and tab["ARZTBRIEF"]["klasse"] == "Dokumente"
    assert lab["pat_id"]["bedeutung"] == "patient_id" and lab["krea_wert"]["loinc"] == "2160-0" and lab["krea_einheit"]["bedeutung"] == "einheit"
    assert lab["kalium"]["bedeutung"] == "messwert" and lab["hb_wert"]["leer"] == 1
    gruppen = {g["art"]: g["spalten"] for g in tab["PAT_LAB_RESULTS"]["gruppen"]}
    assert gruppen["Wert und Einheit"] == ["krea_wert", "krea_einheit"] and "geb_datum" in gruppen["Angaben zur Person"]
    bez = {(b["von"], b["nach"]): b for b in res["beziehungen"]}
    b = bez[("ARZTBRIEF.PATNR", "PAT_LAB_RESULTS.pat_id")]
    assert b["art"] == "vermutet" and "100 %" in b["begruendung"]
    assert "✓ pat_id" in discovery.render_text(res)


def test_ungeklaert_bleibt_beim_menschen_und_fremdschluessel(tmp_path, api):
    db = tmp_path / "x.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE P (PATNR TEXT PRIMARY KEY, XQ7 TEXT); CREATE TABLE F (FALLNR TEXT, PATNR TEXT REFERENCES P(PATNR));"
                      "INSERT INTO P VALUES ('1','a'); INSERT INTO F VALUES ('9','1');")
    con.close()
    url = f"sqlite:///{db}"
    res = discovery.analyse(discovery.crawl(url))
    xq = next(c for c in res["tabellen"][1]["spalten"] if c["name"] == "XQ7")
    assert xq["bedeutung"] == "ungeklaert" and res["zusammenfassung"]["ungeklaert"] == 1
    assert res["beziehungen"] == [{"von": "F.PATNR", "nach": "P.PATNR", "art": "Fremdschlüssel", "sicherheit": "hoch",
                                   "begruendung": "In der Datenbank als Fremdschlüssel erklärt"}]
    call = lambda body: api("POST", "erschliessen", body)    # noqa: E731
    call({"db": url, "entscheidungen": {"P.XQ7": "kommentar", "P.PATNR": "quatsch"}})
    again = call({"db": url})                                    # Entscheidung bleibt gespeichert
    xq = next(c for t in again["tabellen"] for c in t["spalten"] if c["name"] == "XQ7")
    assert xq["bedeutung"] == "kommentar" and xq["quelle"] == "mensch" and again["zusammenfassung"]["ungeklaert"] == 0


def test_externes_modell_nur_mit_freigabe(monkeypatch):
    base = {"url": "https://8.8.8.8/v1", "modell": "x", "api": "openai"}
    with pytest.raises(LLMError, match="nicht im lokalen Netz"):
        LocalLLM(base)
    with pytest.raises(LLMError, match="https"):
        LocalLLM({**base, "url": "http://8.8.8.8/v1", "extern": {"erlaubt": True}})
    meta = LocalLLM({**base, "extern": {"erlaubt": True, "daten": "metadaten"}})
    assert meta.extern and not meta.patientendaten and "nur Metadaten" in meta.label
    assert LocalLLM({**base, "extern": {"erlaubt": True, "daten": "alles"}}).patientendaten
    assert LocalLLM({"url": "http://127.0.0.1:1", "modell": "x"}).patientendaten


def test_schluessel_aus_umgebung_und_kein_dokumenttext_an_metadaten_modell(monkeypatch, cfg, bsp):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    gesehen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"])).decode()
            gesehen.append((self.headers.get("Authorization"), body))
            raw = json.dumps({"message": {"content": '{"zuordnungen":[]}'}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("STELLWERK_LLM_KEY", "geheim")
    llm = LocalLLM({"url": f"http://127.0.0.1:{srv.server_port}", "modell": "t"})
    llm.classify([(0, "x")], [{"key": "a", "titel": "A"}], "a")
    assert gesehen[0][0] == "Bearer geheim"
    # Als "extern, nur Metadaten" markiert: Dokumenttext darf nicht hinausgehen
    from stellwerk.pipeline import ingest, process
    llm.patientendaten = False
    n = len(gesehen)
    res = process(ingest((bsp / "arztbrief_mdm.hl7").read_bytes(), "a.hl7", cfg)[0], cfg, llm=llm)
    assert len(gesehen) == n and "nicht benutzt" in res.llm
    # Erschließung schickt dann nur Namen und Typen, keine Werte
    con = sqlite3.connect(bsp / "y.db")
    con.executescript("CREATE TABLE T (XQ7 TEXT); INSERT INTO T VALUES ('Mustermann-Geheim');")
    con.close()
    discovery.analyse(discovery.crawl(f"sqlite:///{bsp / 'y.db'}"), llm=llm)
    assert len(gesehen) == n + 1 and "XQ7" in gesehen[-1][1] and "Mustermann-Geheim" not in gesehen[-1][1]
    srv.shutdown()
