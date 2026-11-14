"""Erzeugt die Beispieldateien. Alle Personen und Daten sind erfunden.

Aufruf: python beispiele/erzeugen.py [Zielordner]
Braucht reportlab, Pillow und numpy (pip install -e '.[dev,ocr]').
"""
from __future__ import annotations

import base64
import io
import random
import sqlite3
import sys
from pathlib import Path

KOPF = """Musterklinikum Musterstadt · Klinik für Innere Medizin
Musterweg 1, 99999 Musterstadt

Herrn Dr. med. Max Beispiel
Beispielstraße 5, 99999 Musterstadt

Musterstadt, 12.11.2026

Entlassbrief

Sehr geehrter Herr Kollege,

wir berichten über Frau Erika Mustermann, geb. 12.08.1964, die sich vom
05.11.2026 bis 12.11.2026 in unserer stationären Behandlung befand."""

ABSCHNITTE = [
    ("Diagnosen", """Ambulant erworbene Pneumonie rechts basal (J18.9)
Arterielle Hypertonie (I10.90)
Diabetes mellitus Typ 2 (E11.90)"""),
    ("Anamnese", """Die Patientin stellte sich mit seit drei Tagen bestehendem Fieber bis 39,2 °C,
produktivem Husten und Atemnot bei Belastung in unserer Notaufnahme vor.
Vorerkrankungen: Hypertonie und Diabetes, beides hausärztlich eingestellt."""),
    ("Aufnahmebefund", """62-jährige Patientin in reduziertem Allgemeinzustand. RR 145/90 mmHg,
Puls 96/min, Temperatur 38,7 °C, Sauerstoffsättigung 91 % unter Raumluft.
Auskultatorisch feinblasige Rasselgeräusche rechts basal."""),
    ("Labor", """CRP 148 mg/l, Leukozyten 14,2 /nl, Kreatinin 0,9 mg/dl, HbA1c 7,1 %"""),
    ("Therapie und Verlauf", """Unter kalkulierter Antibiose mit Ampicillin/Sulbactam 3 g i.v. dreimal
täglich kam es zur raschen Entfieberung. Das CRP fiel bis zum 11.11.2026 auf 21 mg/l.
Wir konnten die Patientin in gutem Allgemeinzustand nach Hause entlassen."""),
    ("Medikation bei Entlassung", """Ramipril 5 mg 1-0-0
Metformin 1000 mg 1-0-1
Amoxicillin/Clavulansäure 875/125 mg 1-0-1 bis einschließlich 15.11.2026"""),
    ("Empfehlung", """Wir bitten um Kontrolle von CRP und Blutbild in einer Woche sowie um eine
Röntgenkontrolle des Thorax in sechs Wochen."""),
]
SCHLUSS = """Mit freundlichen kollegialen Grüßen

Dr. med. Anna Beispiel, Chefärztin
Dr. med. Jonas Muster, Stationsarzt"""


def brief(ueberschriften: dict[str, str] | None = None) -> str:
    u = ueberschriften or {}
    teile = [KOPF] + [f"{u.get(t, t)}:\n{text}" for t, text in ABSCHNITTE] + [SCHLUSS]
    return "\n\n".join(teile)


def pdf_text(text: str) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    font = "Helvetica"
    dv = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    if dv.exists():
        pdfmetrics.registerFont(TTFont("DejaVu", str(dv)))
        font = "DejaVu"
    y = 800
    c.setFont(font, 10.5)
    for line in text.split("\n"):
        if y < 60:
            c.showPage()
            c.setFont(font, 10.5)
            y = 800
        c.drawString(60, y, line)
        y -= 15
    c.save()
    return buf.getvalue()


def _font(size: int):
    from PIL import ImageFont
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def pdf_scan(text: str, winkel: float = 1.6, rauschen: float = 0.012, seed: int = 7, pro_seite: int = 34) -> bytes:
    """Wie ein mäßiger Scan: Bild ohne Textebene, leicht schräg, verrauscht, unscharf."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter
    rnd = random.Random(seed)
    lines = text.split("\n")
    seiten = [lines[i:i + pro_seite] for i in range(0, len(lines), pro_seite)]
    font = _font(27)
    bilder = []
    for n, zeilen in enumerate(seiten, 1):
        img = Image.new("L", (1654, 2339), 255)
        d = ImageDraw.Draw(img)
        y = 150
        if n > 1:
            d.text((150, 80), "Musterklinikum Musterstadt · Entlassbrief Mustermann, Erika", font=_font(21), fill=40)
        for z in zeilen:
            d.text((150, y), z, font=font, fill=25)
            y += 44
        d.text((720, 2230), f"Seite {n} von {len(seiten)}", font=_font(21), fill=40)
        img = img.rotate(winkel if n % 2 else -winkel, resample=Image.BICUBIC, fillcolor=255)
        img = img.filter(ImageFilter.GaussianBlur(0.9))
        arr = np.asarray(img).astype("int16")
        g = np.random.default_rng(seed + n)
        arr = arr + g.normal(0, 14, arr.shape)
        mask = g.random(arr.shape) < rauschen
        arr[mask] = g.integers(0, 255, int(mask.sum()))
        img = Image.fromarray(np.clip(arr, 0, 255).astype("uint8"))
        bilder.append(img)
        rnd.random()
    buf = io.BytesIO()
    bilder[0].save(buf, format="PDF", save_all=True, append_images=bilder[1:], resolution=200, quality=55)
    return buf.getvalue()


def hl7_mdm() -> bytes:
    """MDM^T02 mit typischen Störungen: MSH-18 nennt UTF-8, geliefert wird Windows-1252 mit doppelt
    kodierten Umlauten in einem Abschnitt, ein Zeilenumbruch mitten im Feld, eine Überschrift, die keine Vorlage kennt."""
    def obx(n, text):
        return f"OBX|{n}|TX|BRIEF^Arztbrief||" + text.replace("\\", "\\E\\").replace("|", "\\F\\").replace("&", "\\T\\").replace("\n", "\\.br\\")

    teile = [KOPF]
    for t, text in ABSCHNITTE:
        teile.append(f"{'Epikrise' if t == 'Therapie und Verlauf' else t}:\n{text}")
    teile.append("Sozialdienst:\nHäusliche Versorgung durch den Ehemann gesichert. Pflegegrad nicht beantragt.")
    teile.append(SCHLUSS)
    segs = ["MSH|^~\\&|ORBIS|MK|STELLWERK|CDR|20261112143000||MDM^T02|MSG00042|P|2.5|||||DE|UNICODE UTF-8",
            "EVN|T02|20261112143000",
            "PID|1||4711001^^^MK^PI||Mustermann^Erika||19640812|F|||Musterweg 1^^Musterstadt^^99999^DE",
            "PV1|1|I|N3A^312^1|||||||INN|||||||||2026004711",
            "TXA|1|EB|TX|20261112141500|||||^Beispiel^Anna^^^Dr. med.|||DOK-2026-000815|||||AU"]
    raw_segs = [s.encode("cp1252") for s in segs]
    for n, t in enumerate(teile, 1):
        line = obx(n, t)
        if t.startswith("Anamnese"):                             # doppelt kodiert: UTF-8 als Windows-1252 gelesen
            raw_segs.append(line.encode("utf-8").decode("cp1252").encode("cp1252"))
        elif t.startswith("Empfehlung"):                         # echter Zeilenumbruch im Feld
            raw_segs.append(line.replace("\\.br\\", "\r", 1).replace("\\.br\\", "\\.br\\").encode("cp1252"))
        else:
            raw_segs.append(line.encode("cp1252"))
    return b"\r".join(raw_segs) + b"\r"


def hl7_mit_pdf(pdf: bytes) -> bytes:
    b64 = base64.b64encode(pdf).decode("ascii")
    segs = ["MSH|^~\\&|ARCHIV|MK|STELLWERK|CDR|20261112150000||MDM^T02|MSG00043|P|2.5|||||DE|8859/1",
            "PID|1||4711001^^^MK^PI||Mustermann^Erika||19640812|F",
            "PV1|1|I|N3A^312^1|||||||INN|||||||||2026004711",
            "TXA|1|EB|AP|20261112141500|||||^Beispiel^Anna^^^Dr. med.|||DOK-2026-000816|||||AU",
            f"OBX|1|ED|BRIEF^Arztbrief||^application^pdf^Base64^{b64}"]
    return ("\r".join(segs) + "\r").encode("latin-1")


def kis_db(path: Path) -> None:
    """Kleine KIS-ähnliche Tabelle: je Abschnitt eine Spalte, ein Feld als RTF, eins mit kaputten Umlauten."""
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE ARZTBRIEF (DOKNR TEXT, PATNR TEXT, FALLNR TEXT, FALLART TEXT, NACHNAME TEXT, VORNAME TEXT,
                   GEBDAT TEXT, GESCHLECHT TEXT, DOKDATUM TEXT, ARZT TEXT, DIAGNOSEN TEXT, ANAMNESE_TXT TEXT,
                   EPIKRISE TEXT, MEDIKATION TEXT, EMPFEHLUNG TEXT, INTERN_VERMERK TEXT)""")
    a = dict(ABSCHNITTE)
    rtf = ("{\\rtf1\\ansi\\ansicpg1252\\deff0{\\fonttbl{\\f0 Arial;}}\\f0\\fs20 "
           + a["Therapie und Verlauf"].replace("\n", "\\par ").encode("cp1252").decode("latin-1")
           .replace("ä", "\\'e4").replace("ü", "\\'fc").replace("ö", "\\'f6").replace("ß", "\\'df") + "\\par}")
    kaputt = a["Anamnese"].encode("utf-8").decode("cp1252")
    con.execute("INSERT INTO ARZTBRIEF VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("DOK-2026-000817", "4711001", "2026004711", "S", "Mustermann", "Erika", "12.08.1964", "W",
                 "12.11.2026 14:15", "Dr. med. Anna Beispiel", a["Diagnosen"], kaputt, rtf,
                 a["Medikation bei Entlassung"], a["Empfehlung"], "Brief am 12.11. diktiert"))
    con.execute("INSERT INTO ARZTBRIEF VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("DOK-2026-000818", "4711002", "2026004712", "A", "Beispiel", "Hans", "03.02.1951", "M",
                 "13.11.2026", "Dr. med. Jonas Muster", "Z. n. Synkope unklarer Genese (R55)", "Patient kam nach einmaligem Kollaps.",
                 "Unauffälliger Verlauf am Monitor.", "Keine Dauermedikation", "Langzeit-EKG ambulant.", ""))
    con.execute("""CREATE TABLE PAT_LAB_RESULTS (auftrag_nr TEXT, pat_id TEXT, fall_nr TEXT, nachname TEXT, vorname TEXT, geb_datum TEXT,
                   geschlecht TEXT, abnahme_zeit TEXT, krea_wert TEXT, krea_einheit TEXT, kalium TEXT, crp TEXT, hb_wert TEXT, kommentar TEXT)""")
    lab = [("A26-0001", "4711001", "2026004711", "Mustermann", "Erika", "12.08.1964", "W", "06.11.2026 07:40", "1,10", "mg/dl", "4,1", "148,0", "13,2", ""),
           ("A26-0002", "4711001", "2026004711", "Mustermann", "Erika", "12.08.1964", "W", "11.11.2026 07:35", "0,90", "mg/dl", "4,4", "21,0", "12,9", "Kontrolle"),
           ("A26-0003", "4711002", "2026004712", "Beispiel", "Hans", "03.02.1951", "M", "13.11.2026 08:10", "97", "µmol/l", "3,9", "<0,6", None, ""),
           ("A26-0004", "4711003", "2026004713", "Muster", "Kim", "21.05.1990", "X", "13.11.2026 09:02", "0,84", "mg/dl", None, "3,2", "14,8", "")]
    con.executemany("INSERT INTO PAT_LAB_RESULTS VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", lab)
    con.commit()
    con.close()


ZUORDNUNG = """# Beispiel: Arztbriefe aus einer KIS-ähnlichen Tabelle, je Abschnitt eine Spalte.
quelle:
  db: sqlite:///kis.db
  abfrage: |
    SELECT DOKNR, PATNR, FALLNR, FALLART, NACHNAME, VORNAME, GEBDAT, GESCHLECHT, DOKDATUM, ARZT,
           DIAGNOSEN, ANAMNESE_TXT, EPIKRISE, MEDIKATION, EMPFEHLUNG, INTERN_VERMERK
    FROM ARZTBRIEF ORDER BY DOKNR
vorlage: entlassbrief
# Was Stellwerk am Spaltennamen erkennt, muss hier nicht stehen.
# Nur Abweichungen festschreiben:
spalten: {}
abschnitte: {}
"""


def main(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    text = brief()
    (target / "arztbrief_mdm.hl7").write_bytes(hl7_mdm())
    textpdf = pdf_text(text)
    (target / "arztbrief_text.pdf").write_bytes(textpdf)
    (target / "arztbrief_scan.pdf").write_bytes(pdf_scan(text))
    (target / "arztbrief_mdm_mit_pdf.hl7").write_bytes(hl7_mit_pdf(textpdf))
    kis_db(target / "kis.db")
    (target / "kis_zuordnung.yaml").write_text(ZUORDNUNG, encoding="utf-8")
    print(f"Beispiele erzeugt in {target}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent)
