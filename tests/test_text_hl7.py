from stellwerk import hl7v2
from stellwerk.textrepair import clean_text, decode_bytes, fix_mojibake, rtf_to_text, strip_markup


def test_utf8_trotz_falscher_angabe():
    t, f = decode_bytes("Müller".encode("utf-8"), "8859/1")
    assert t == "Müller" and f[0].code == "ENC_DEKLARIERT_FALSCH"


def test_windows1252_trotz_utf8_angabe():
    t, f = decode_bytes("Größe 5 € – „gut“".encode("cp1252"), "UNICODE UTF-8")
    assert t == "Größe 5 € – „gut“" and f[0].code == "ENC_ERKANNT"


def test_dos_zeichensatz_wird_erkannt():
    t, f = decode_bytes("Männer über fünfzig, Größe".encode("cp850"), None)
    assert t == "Männer über fünfzig, Größe" and f[0].severity == "pruefen"


def test_gemischte_zeilen():
    data = "Größe\r".encode("utf-8") + "Übelkeit\r".encode("cp1252")
    t, f = decode_bytes(data)
    assert "Größe" in t and "Übelkeit" in t and f[0].code == "ENC_GEMISCHT"


def test_mojibake_einfach_und_doppelt():
    kaputt = "Größe, Übelkeit, 37 °C, 5 €".encode("utf-8").decode("cp1252")
    assert fix_mojibake(kaputt)[0] == "Größe, Übelkeit, 37 °C, 5 €"
    doppelt = "Müller".encode("utf-8").decode("cp1252").encode("utf-8").decode("cp1252")
    assert fix_mojibake(doppelt)[0] == "Müller"


def test_medizinische_zeichen_bleiben():
    t, _ = clean_text("5 µg/m²  x­yﬁ\x07")
    assert t == "5 µg/m²  xyfi"


def test_zerstoerte_zeichen_werden_gemeldet_nicht_geraten():
    t, f = clean_text("M?ller wohnt in der Stra�e")
    assert "M?ller" in t and {x.code for x in f} == {"ENC_VERLOREN", "ENC_FRAGEZEICHEN"}


def test_rtf_und_html():
    rtf = "{\\rtf1\\ansi\\ansicpg1252{\\fonttbl{\\f0 Arial;}}\\f0 Gr\\'f6\\'dfe\\par Zeile 2 \\" + "u8364?}"
    assert rtf_to_text(rtf) == "Größe\nZeile 2 €"
    assert strip_markup("<p>Eins &amp; zwei</p><p>Drei<br>Vier</p>")[0] == "Eins & zwei\n\nDrei\nVier"


MSG = ("MSH|^~\\&|LAB|MK|X|Y|20260704101500||MDM^T02|C1|P|2.5\r"
       "PID|1||99^^^MK^XX~4711^^^MK^PI||M\\T\\M^Eva||19640812|D\r"
       "PV1|1|O||||||||||||||||||F1\r"
       "TXA|1|EB|TX|20260704101500|||||^Arzt^Ada^^^Dr.|||D1|||||IP\r"
       "OBX|1|TX|||Diagnose: A \\F\\ B\\.br\\Zeile 2~Zeile 3\r"
       "fortgesetzt\r").encode("ascii")


def test_hl7_kopfdaten_und_text(cfg):
    doc = hl7v2.document_from_message(hl7v2.parse(MSG), "t", cfg)
    m = doc.meta
    assert (m.patient_id, m.family, m.gender, m.gender_amtlich, m.encounter_class) == ("4711", "M&M", "other", "D", "AMB")
    assert m.doc_date == "2026-07-04T10:15:00+02:00" and m.author == "Dr. Ada Arzt" and m.doc_id == "D1"
    assert [ln.text for ln in doc.lines] == ["Diagnose: A | B", "Zeile 2", "Zeile 3", "fortgesetzt"]
    codes = {f.code for f in doc.findings}
    assert {"HL7_UMBRUCH_IM_FELD", "HL7_NICHT_FINAL", "HL7_ZEITZONE"} <= codes


def test_hl7_winterzeit_und_falscher_typ(cfg):
    assert hl7v2.ts_to_fhir("20261112141500")[0] == "2026-11-12T14:15:00+01:00"
    doc = hl7v2.document_from_message(hl7v2.parse(b"MSH|^~\\&|A|B|C|D|2026||ADT^A01|1|P|2.5\rPID|1||1\r"), "t", cfg)
    assert any(f.code == "HL7_TYP" and f.severity == "fehler" for f in doc.findings)


def test_mllp_rahmen_und_mehrere_nachrichten():
    data = b"\x0b" + MSG + b"\x1c\r\x0b" + MSG + b"\x1c\r"
    assert len(hl7v2.split_messages(data)) == 2


def test_ack():
    a = hl7v2.ack(MSG).decode()
    assert a.startswith("MSH|^~\\&|X|Y|LAB|MK|") and "|ACK^T02|" in a and "MSA|AA|C1" in a
    assert b"MSA|AR" in hl7v2.ack(b"quatsch")
