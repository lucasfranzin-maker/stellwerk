import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from stellwerk.llm import LLMError, LocalLLM
from stellwerk.model import Line, SourceDoc
from stellwerk.sections import load_template, sectionize

TPL = load_template("entlassbrief")


def doc(text):
    return SourceDoc("text", "t", lines=[Line(x) for x in text.split("\n")])


def by_key(secs):
    return {s.key: [ln.text for ln in s.lines if ln.text.strip()] for s in secs}


def test_ueberschriften_exakt_inline_und_verlesen():
    secs, f = sectionize(doc("Sehr geehrte Kollegen,\n\nDiagnosen:\nHypertonie\n\nMedikati0n\nRamipril 5 mg 1-0-0\n"
                             "Procedere: Kontrolle in 2 Wochen\n\nMit freundlichen Grüßen\nDr. X"), TPL)
    k = by_key(secs)
    assert k["anschreiben"] == ["Sehr geehrte Kollegen,"] and k["diagnosen"] == ["Hypertonie"]
    assert k["medikation"] == ["Ramipril 5 mg 1-0-0"] and k["empfehlung"] == ["Kontrolle in 2 Wochen"]
    assert k["schluss"] == ["Mit freundlichen Grüßen", "Dr. X"]
    assert [x.code for x in f] == ["ABS_UNSCHARF"]


def test_unbekannte_ueberschrift_geht_nicht_verloren():
    secs, f = sectionize(doc("Diagnosen:\nA\n\nSozialdienst:\nVersorgung gesichert"), TPL)
    assert by_key(secs)["weitere"] == ["Versorgung gesichert"]
    assert secs[-1].parts[0].heading == "Sozialdienst"
    assert any(x.code == "ABS_UNBEKANNT" and x.detail["alias"] == "Sozialdienst" for x in f)


def test_ohne_gliederung_und_pflichtabschnitt():
    secs, f = sectionize(doc("Der Patient kam.\n\nEr ging wieder."), TPL)
    assert [s.key for s in secs] == ["weitere"]
    assert {"ABS_OHNE_GLIEDERUNG", "ABS_FEHLT"} == {x.code for x in f}


def test_gliederung_aus_der_quelle():
    d = SourceDoc("hl7", "t", lines=[Line("I10", hint="Diagnosen"), Line("kam zu Fuß", hint="Anamnese"), Line("x", hint="Kürzel7")])
    k = by_key(sectionize(d, TPL)[0])
    assert k == {"diagnosen": ["I10"], "anamnese": ["kam zu Fuß"], "weitere": ["x"]}


class Stub(BaseHTTPRequestHandler):
    antwort = None
    gesehen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        self._out({"models": [{"name": "test:latest"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Stub.gesehen.append((self.path, body))
        content = Stub.antwort
        self._out({"message": {"content": content}} if self.path == "/api/chat" else {"choices": [{"message": {"content": content}}]})

    def _out(self, obj):
        raw = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def stub():
    srv = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    Stub.gesehen = []
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.mark.parametrize("api", ["ollama", "openai"])
def test_modell_schlaegt_nur_zuordnung_vor(stub, api):
    Stub.antwort = '<think>hm</think>{"zuordnungen":[{"i":2,"abschnitt":"verlauf","sicherheit":"hoch"},{"i":99,"abschnitt":"verlauf","sicherheit":"hoch"}]}'
    llm = LocalLLM({"url": stub, "modell": "test", "api": api})
    secs, f = sectionize(doc("Diagnosen:\nA\n\nZusammenschau:\nIm Verlauf komplikationslos."), TPL, llm)
    assert by_key(secs)["verlauf"] == ["Im Verlauf komplikationslos."]
    ki = next(x for x in f if x.code == "ABS_KI")
    assert ki.severity == "pruefen" and "Stichwörter" in ki.message and ki.detail == {"alias": "Zusammenschau", "abschnitt": "verlauf"}
    path, body = Stub.gesehen[0]
    assert path == ("/api/chat" if api == "ollama" else "/v1/chat/completions")
    assert "Im Verlauf" in body["messages"][1]["content"]


def test_modell_antwortet_unsinn_dann_ohne_modell_weiter(stub):
    Stub.antwort = "Gern! Hier ist der umgeschriebene Brief ..."
    llm = LocalLLM({"url": stub, "modell": "test"})
    secs, f = sectionize(doc("Diagnosen:\nA\n\nZusammenschau:\nText"), TPL, llm)
    assert by_key(secs)["weitere"] == ["Text"]
    assert {"LLM_FEHLER", "ABS_UNBEKANNT"} <= {x.code for x in f}


def test_modell_ohne_gliederung(stub):
    Stub.antwort = '{"zuordnungen":[{"i":0,"abschnitt":"anamnese","sicherheit":"mittel"},{"i":1,"abschnitt":"medikation","sicherheit":"hoch"}]}'
    secs, f = sectionize(doc("Kam mit Fieber.\n\nRamipril 5 mg 1-0-0"), TPL, LocalLLM({"url": stub, "modell": "test"}))
    assert by_key(secs) == {"anamnese": ["Kam mit Fieber."], "medikation": ["Ramipril 5 mg 1-0-0"]}
    assert any(x.code == "ABS_KI_OHNE_GLIEDERUNG" for x in f)


def test_fremder_server_wird_abgelehnt():
    with pytest.raises(LLMError, match="nicht im lokalen Netz"):
        LocalLLM({"url": "https://8.8.8.8/v1", "modell": "x"})
    LocalLLM({"url": "https://8.8.8.8/v1", "modell": "x", "erlaubte_hosts": ["8.8.8.8"]})


def test_selbsttest(stub):
    Stub.antwort = '{"zuordnungen":[{"i":1,"abschnitt":"medikation","sicherheit":"hoch"},{"i":2,"abschnitt":"diagnosen","sicherheit":"hoch"},{"i":3,"abschnitt":"empfehlung","sicherheit":"hoch"}]}'
    assert LocalLLM({"url": stub, "modell": "test"}).selftest()[0]
