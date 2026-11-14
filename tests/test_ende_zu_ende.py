import asyncio
import json
import threading
import urllib.request
from datetime import datetime, timezone

import pytest
from fhir.resources.R4B.bundle import Bundle

from stellwerk import cli
from stellwerk.mllp import handle_message
from stellwerk.pipeline import deliver, ingest, process, write_outputs
from stellwerk.server import serve
from stellwerk.sqlsource import SqlError, check_readonly

NOW = datetime(2026, 11, 14, 6, 0, tzinfo=timezone.utc)


def run(path, cfg, meta=None, **kw):
    return [process(d, cfg, now=NOW, **kw) for d in ingest(path.read_bytes(), path.name, cfg, meta)]


def codes(res):
    return {f.code for f in res.findings}


def test_hl7_zu_gueltigem_isik_dokument(bsp, cfg):
    res = run(bsp / "arztbrief_mdm.hl7", cfg)[0]
    assert res.status == "pruefen" and {"ENC_GEMISCHT", "ABS_UNBEKANNT"} <= codes(res)
    b = res.bundle
    Bundle.model_validate(b)                                    # Grundregeln von FHIR
    comp = b["entry"][0]["resource"]
    assert b["type"] == "document" and comp["text"]["status"] == "extensions"
    assert comp["subject"]["reference"] == b["entry"][1]["fullUrl"] and comp["encounter"]["reference"] == b["entry"][2]["fullUrl"]
    titel = [s["title"] for s in comp["section"]]
    assert titel[:3] == ["Anschreiben", "Diagnosen", "Anamnese"] and "Verlauf" in titel
    anamnese = comp["section"][2]["text"]["div"]
    assert "39,2 °C" in anamnese and "Ã" not in json.dumps(b, ensure_ascii=False)
    assert "Mustermann" in comp["text"]["div"] and "12.08.1964" in comp["text"]["div"]
    assert run(bsp / "arztbrief_mdm.hl7", cfg)[0].bundle == b     # gleiche Eingabe, gleiches Bundle


def test_sonderzeichen_im_narrativ(cfg):
    raw = ("MSH|^~\\&|A|B|C|D|20261112||MDM^T02|1|P|2.5\rPID|1||7||X^Y||19500101|M\rPV1|1|I|||||||||||||||||F\r"
           "TXA|1|EB|TX|20261112||||||||D9\rOBX|1|TX|||Diagnosen:~pO2 < 60 \\T\\ pCO2 > 45 <script>\r").encode()
    res = process(ingest(raw, "t", cfg)[0], cfg, now=NOW)
    div = res.bundle["entry"][0]["resource"]["section"][0]["text"]["div"]
    assert "pO2 &lt; 60 &amp; pCO2 &gt; 45 &lt;script&gt;" in div and not any(f.code == "FHIR_REGEL" for f in res.findings)


def test_transaktion(bsp, cfg):
    cfg["ziel"] = "transaktion"
    b = run(bsp / "arztbrief_mdm.hl7", cfg)[0].bundle
    Bundle.model_validate(b)
    req = {e["resource"]["resourceType"]: e["request"] for e in b["entry"]}
    assert req["Patient"]["ifNoneExist"].startswith("identifier=https%3A%2F%2Fhaus.example") and req["Composition"]["method"] == "PUT"


def test_pdf_mit_textebene_und_eingebettet(bsp, cfg):
    res = run(bsp / "arztbrief_mdm_mit_pdf.hl7", cfg)[0]
    assert res.doc.original_type == "application/pdf" and not res.doc.stats.get("ocr_seiten")
    assert res.status == "ok", [f.message for f in res.findings if f.severity != "info"]
    assert len(res.sections) == 9


def test_schlechter_scan(bsp, cfg):
    cfg["standard"]["fallart"] = "stationaer"
    meta = {"patient_id": "4711001", "encounter_id": "F1", "doc_id": "S1", "doc_date": "2026-11-12", "author": "Dr. B",
            "family": "Mustermann", "birth_date": "1964-08-12"}
    res = run(bsp / "arztbrief_scan.pdf", cfg, meta)[0]
    assert res.doc.stats["ocr_seiten"] == 2 and res.doc.stats["ocr_sicherheit"] > 80
    keys = {s.key for s in res.sections}
    assert {"diagnosen", "anamnese", "medikation", "empfehlung", "schluss"} <= keys
    assert "PDF_OCR_ZAHL" in codes(res) and res.status == "pruefen"
    assert "Texterkennung" in res.bundle["entry"][0]["resource"]["text"]["div"]
    Bundle.model_validate(res.bundle)


def test_falscher_patient_faellt_auf(bsp, cfg):
    meta = {"patient_id": "1", "doc_id": "S2", "birth_date": "1946-08-12"}
    assert "PAT_ABWEICHUNG" in codes(run(bsp / "arztbrief_text.pdf", cfg, meta)[0])


def test_ohne_patientennummer_kein_bundle(bsp, cfg):
    res = run(bsp / "arztbrief_text.pdf", cfg)[0]
    assert res.status == "fehler" and res.bundle is None and "META_PATIENT" in codes(res)


def test_sql_cli_und_dateiablage(bsp, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    ziel = tmp_path / "cloverleaf_in"
    rc = cli.main(["sql", str(bsp / "kis_zuordnung.yaml"), "--out", "out", "--zustellen", str(ziel), "--trotz-pruefung", "--ohne-llm"])
    out = capsys.readouterr().out
    assert rc == 1 and "INTERN_VERMERK" in out and "Gilt für alle 2 Dokumente" in out
    files = sorted(p.name for p in ziel.iterdir())
    assert files == ["DOK-2026-000817.json", "DOK-2026-000818.json"]
    comp = json.loads((ziel / files[0]).read_text("utf-8"))["entry"][0]["resource"]
    text = json.dumps(comp, ensure_ascii=False)
    assert "39,2 °C" in text and "rtf" not in text and "Entfieberung" in text and comp["date"] == "2026-11-12T14:15:00+01:00"
    assert (tmp_path / "out" / "DOK-2026-000817" / "bericht.html").read_text("utf-8").startswith("<!doctype html>")


def test_zurueckhalten_bei_pruefpunkten(bsp, cfg, tmp_path):
    res = run(bsp / "arztbrief_mdm.hl7", cfg)[0]
    deliver(res, cfg, str(tmp_path / "ziel"))
    assert not res.delivered and "zurückgehalten" in res.delivery and not (tmp_path / "ziel").exists()


def test_nur_lesende_abfragen():
    check_readonly("WITH x AS (SELECT 1) SELECT * FROM x -- drop")
    for q in ("DELETE FROM a", "SELECT 1; DROP TABLE a", "select * from a where 1=1; update a set b=1"):
        with pytest.raises(SqlError):
            check_readonly(q)


def test_mllp_empfang(bsp, cfg, tmp_path):
    ackmsg, results = handle_message((bsp / "arztbrief_mdm.hl7").read_bytes(), cfg, str(tmp_path))
    assert b"MSA|AA|MSG00042" in ackmsg and results[0].bundle and list((tmp_path / "eingang").iterdir())
    assert b"MSA|AR" in handle_message(b"kein hl7", cfg, str(tmp_path))[0]


def test_mllp_ueber_die_leitung(bsp, cfg, tmp_path):
    from stellwerk import mllp

    async def go():
        port = []
        task = asyncio.create_task(mllp.serve("127.0.0.1", 0, cfg, str(tmp_path), ready=port.append))
        while not port:
            await asyncio.sleep(0.01)
        r, w = await asyncio.open_connection("127.0.0.1", port[0])
        w.write(b"\x0b" + (bsp / "arztbrief_mdm.hl7").read_bytes() + b"\x1c\r")
        data = await asyncio.wait_for(r.readuntil(b"\x1c\r"), 20)
        w.close()
        task.cancel()
        return data
    assert b"MSA|AA|MSG00042" in asyncio.run(go())


def test_http_dienst(bsp, cfg, tmp_path):
    srv = serve("127.0.0.1", 0, cfg, str(tmp_path))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    req = urllib.request.Request(base + "/v1/convert?name=a.hl7", data=(bsp / "arztbrief_mdm.hl7").read_bytes(), method="POST")
    out = json.loads(urllib.request.urlopen(req, timeout=30).read())
    assert out["status"] == "pruefen" and out["dokumente"][0]["bundle"]["resourceType"] == "Bundle"
    assert b"Stellwerk" in urllib.request.urlopen(base + "/", timeout=5).read()
    srv.shutdown()
