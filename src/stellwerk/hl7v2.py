"""Toleranter HL7-v2-Leser für Dokumentnachrichten (MDM, ORU).

Liest, was in der Praxis ankommt: MLLP-Rahmen, falsche Zeichensatzangaben,
Zeilenumbrüche mitten im Feld, Escape-Sequenzen, eingebettete PDFs.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from .model import FEHLER, INFO, PRUEFEN, Finding, Line, SourceDoc, zahl
from .textrepair import clean_text, decode_bytes, rtf_to_text

_SEG_START = re.compile(r"^[A-Z][A-Z0-9]{2}$")


@dataclass
class Message:
    segments: list[list[str]] = field(default_factory=list)   # je Segment: [Name, Feld1, Feld2, ...]
    fs: str = "|"
    cs: str = "^"
    rs: str = "~"
    esc: str = "\\"
    sub: str = "&"
    findings: list[Finding] = field(default_factory=list)

    def all(self, name: str) -> list[list[str]]:
        return [s for s in self.segments if s[0] == name]

    def first(self, name: str) -> list[str] | None:
        return next((s for s in self.segments if s[0] == name), None)

    def get(self, name: str, n: int, comp: int = 0, seg: list[str] | None = None) -> str:
        """Feld n (HL7-Zählung). comp > 0 liefert die Komponente der ersten Wiederholung, entschlüsselt."""
        seg = seg or self.first(name)
        if not seg:
            return ""
        idx = n - 1 if seg[0] == "MSH" else n
        val = seg[idx] if 0 < idx < len(seg) else ""
        if comp:
            parts = val.split(self.rs)[0].split(self.cs)
            val = parts[comp - 1] if comp <= len(parts) else ""
            val = val.split(self.sub)[0]
        return self.unescape(val) if comp else val

    def unescape(self, value: str) -> str:
        if self.esc not in value:
            return value
        e = re.escape(self.esc)

        def repl(m: re.Match) -> str:
            body = m.group(1)
            simple = {"F": self.fs, "S": self.cs, "T": self.sub, "R": self.rs, "E": self.esc,
                      "H": "", "N": "", ".br": "\n", ".fi": "", ".nf": "", ".ce": "\n"}
            if body in simple:
                return simple[body]
            mm = re.fullmatch(r"\.sp ?\+?(\d*)", body)
            if mm:
                return "\n" * int(mm.group(1) or 1)
            mm = re.fullmatch(r"\.(?:in|ti|sk) ?[+-]?(\d*)", body)
            if mm:
                return " " * int(mm.group(1) or 0) if body.startswith(".sk") else ""
            mm = re.fullmatch(r"X((?:[0-9A-Fa-f]{2})+)", body)
            if mm:
                raw = bytes.fromhex(mm.group(1))
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return raw.decode("cp1252", errors="replace")
            return m.group(0)

        return re.sub(e + r"(\.[a-z]{2}(?: ?[+-]?\d+)?|[A-Z](?:[0-9A-Fa-f]{2})*)" + e, repl, value)


def split_messages(data: bytes) -> list[bytes]:
    """MLLP-Rahmen entfernen und eine Datei mit mehreren Nachrichten zerlegen."""
    data = data.replace(b"\x0b", b"").replace(b"\x1c\r", b"\r").replace(b"\x1c", b"\r")
    parts = re.split(rb"(?:(?<=[\r\n])|^)(?=MSH[^A-Za-z0-9\s])", data)
    return [p for p in parts if p.strip().startswith(b"MSH")]


def parse(data: bytes, where: str = "HL7") -> Message:
    msg = Message()
    head = data.lstrip()[:400]
    if not head.startswith(b"MSH") or len(head) < 8:
        msg.findings.append(Finding("HL7_KEIN_MSH", FEHLER, where, "Keine HL7-Nachricht: Das Segment MSH fehlt.",
                                    "Datei prüfen. Erwartet wird eine Nachricht, die mit „MSH|“ beginnt."))
        return msg
    fs = chr(head[3])
    first = re.split(rb"[\r\n]", head, maxsplit=1)[0].split(head[3:4])
    declared = first[17].decode("ascii", "replace").split("^")[0].split("~")[0] if len(first) > 17 else ""
    text, f1 = decode_bytes(data, declared or None, f"{where} MSH-18")
    text, f2 = clean_text(text, where)
    msg.findings += f1 + f2

    enc = text.lstrip()[4:9]
    msg.fs = fs
    msg.cs, msg.rs, msg.esc, msg.sub = (list(enc.split(fs)[0][:4]) + list("^~\\&"))[:4] if enc else list("^~\\&")
    glued = 0
    for raw in text.split("\n"):
        if not raw.strip():
            continue
        name = raw[:3]
        if _SEG_START.match(name) and (len(raw) == 3 or raw[3] == fs):
            fields = raw.split(fs)
            msg.segments.append(fields)
        elif msg.segments:                       # Zeilenumbruch mitten im Feld: an das letzte Feld anhängen
            cont = raw.split(fs)
            msg.segments[-1][-1] += "\n" + cont[0]
            msg.segments[-1].extend(cont[1:])
            glued += 1
    if glued:
        msg.findings.append(Finding(
            "HL7_UMBRUCH_IM_FELD", INFO, where,
            (f"{glued} Zeilen gehören zu keinem Segment und wurden" if glued != 1 else "1 Zeile gehört zu keinem Segment und wurde")
            + " als Fortsetzung des vorigen Feldes gelesen "
            "(unmaskierter Zeilenumbruch im Text).",
            "Sender sollte Zeilenumbrüche als \\.br\\ oder als Wiederholung (~) schicken."))
    return msg


# --------------------------------------------------------------------------
# Werte umrechnen
# --------------------------------------------------------------------------

_TS = re.compile(r"^(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?(?:\.\d+)?([+-]\d{4})?$")


def ts_to_fhir(ts: str, tz: str = "Europe/Berlin") -> tuple[str, bool]:
    """HL7-Zeitstempel -> FHIR date/dateTime. Zweiter Wert: Zeitzone wurde angenommen."""
    m = _TS.match((ts or "").strip())
    if not m:
        return "", False
    y, mo, d, h, mi, s, off = m.groups()
    if not mo:
        return y, False
    if not d:
        return f"{y}-{mo}", False
    if not h:
        return f"{y}-{mo}-{d}", False
    try:
        if off:
            return f"{y}-{mo}-{d}T{h}:{mi or '00'}:{s or '00'}{off[:3]}:{off[3:]}", False
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi or 0), int(s or 0), tzinfo=ZoneInfo(tz))
        return dt.isoformat(), True
    except ValueError:
        return "", False


_GENDER = {"M": "male", "F": "female", "W": "female", "U": "unknown", "O": "other", "A": "other",
           "D": "other", "X": "other"}
_CLASS = {"I": "IMP", "O": "AMB", "E": "EMER", "P": "PRENC", "R": "IMP", "B": "IMP"}
_NICHT_FINAL = {"DI": "diktiert", "IP": "in Bearbeitung", "IN": "unvollständig", "PA": "vorläufig freigegeben",
                "P": "vorläufig", "I": "ausstehend", "R": "noch nicht geprüft", "S": "teilweise"}


def _xcn(msg: Message, val: str) -> str:
    """Arztangabe (XCN) als lesbarer Name."""
    c = [msg.unescape(x.split(msg.sub)[0]) for x in val.split(msg.rs)[0].split(msg.cs)]
    c += [""] * 8
    name = " ".join(x for x in (c[5], c[2], c[1]) if x).strip()
    return name or c[0]


def _base64_payload(val: str) -> bytes | None:
    cleaned = re.sub(r"[^A-Za-z0-9+/=]", "", val)
    try:
        return base64.b64decode(cleaned + "=" * (-len(cleaned) % 4))
    except Exception:
        return None


def document_from_message(msg: Message, name: str, cfg: dict) -> SourceDoc:
    """Dokument samt Kopfdaten aus einer MDM- oder ORU-Nachricht lesen."""
    doc = SourceDoc(kind="hl7", name=name, findings=list(msg.findings))
    if not msg.segments:
        return doc
    tz = cfg.get("zeitzone", "Europe/Berlin")
    mtype = msg.get("MSH", 9, 1) + "^" + msg.get("MSH", 9, 2)
    doc.stats["nachrichtentyp"] = mtype
    doc.stats["kontroll_id"] = msg.get("MSH", 10)
    m = doc.meta
    m.source_system = msg.get("MSH", 3, 1) or msg.get("MSH", 4, 1)

    if not mtype.startswith(("MDM", "ORU")):
        doc.findings.append(Finding(
            "HL7_TYP", FEHLER, "MSH-9", f"Nachrichtentyp {mtype} enthält kein Dokument.",
            "Stellwerk verarbeitet MDM (z. B. T02) und ORU^R01. Andere Typen im Kommunikationsserver vorher ausfiltern."))
        return doc

    # --- Patient
    pid3 = msg.get("PID", 3)
    reps = [r.split(msg.cs) + [""] * 5 for r in pid3.split(msg.rs) if r]
    chosen = next((r for r in reps if r[4] in ("PI", "MR")), reps[0] if reps else None)
    if chosen:
        m.patient_id = msg.unescape(chosen[0])
    m.family = msg.get("PID", 5, 1)
    m.given = msg.get("PID", 5, 2)
    m.birth_date = ts_to_fhir(msg.get("PID", 7)[:8])[0]
    sex = msg.get("PID", 8).strip().upper()
    if sex:
        m.gender = _GENDER.get(sex, "unknown")
        if sex in ("D", "X"):
            m.gender_amtlich = sex
        if sex not in _GENDER:
            doc.findings.append(Finding("HL7_GESCHLECHT", INFO, "PID-8",
                                        f"Unbekannter Geschlechtscode „{sex}“, als „unknown“ übernommen."))
    m.encounter_id = msg.get("PV1", 19, 1)
    m.encounter_class = _CLASS.get(msg.get("PV1", 2).strip().upper(), "")

    # --- Dokumentkopf
    assumed_tz = False
    status = ""
    if msg.first("TXA"):
        m.type_hint = msg.get("TXA", 2, 1)
        m.doc_date, assumed_tz = ts_to_fhir(msg.get("TXA", 4) or msg.get("TXA", 6), tz)
        m.doc_id = msg.get("TXA", 12, 1)
        m.author = _xcn(msg, msg.get("TXA", 9) or msg.get("TXA", 5))
        m.title = msg.get("TXA", 16, 1) if not re.search(r"\.\w{2,4}$", msg.get("TXA", 16, 1)) else ""
        status = msg.get("TXA", 17).strip().upper()
    obr = msg.first("OBR")
    if obr:
        m.type_hint = m.type_hint or msg.get("OBR", 4, 1)
        m.title = m.title or msg.get("OBR", 4, 2)
        if not m.doc_date:
            m.doc_date, assumed_tz = ts_to_fhir(msg.get("OBR", 7) or msg.get("OBR", 22), tz)
        m.doc_id = m.doc_id or msg.get("OBR", 3, 1)
        if not m.author:
            o32 = msg.get("OBR", 32).split(msg.cs)[0].split(msg.sub) + ["", "", ""]
            m.author = " ".join(x for x in (o32[2], o32[1]) if x).strip()
        status = status or msg.get("OBR", 25).strip().upper()
    if not m.doc_date:
        m.doc_date, assumed_tz = ts_to_fhir(msg.get("MSH", 7), tz)
        if m.doc_date:
            doc.findings.append(Finding("HL7_DATUM_ERSATZ", INFO, "MSH-7",
                                        "Kein Dokumentdatum in TXA-4/OBR-7; Nachrichtenzeit aus MSH-7 verwendet."))
    if assumed_tz:
        doc.findings.append(Finding("HL7_ZEITZONE", INFO, "Zeitstempel",
                                    f"Zeitstempel ohne Zeitzone geliefert; {tz} angenommen."))
    if not m.doc_id and msg.get("MSH", 10):
        m.doc_id = msg.get("MSH", 10)
        doc.findings.append(Finding(
            "HL7_DOKID_ERSATZ", PRUEFEN, "MSH-10",
            "Keine Dokumentnummer in TXA-12/OBR-3; Nachrichten-Kontroll-ID als Dokumentnummer verwendet.",
            "Eine Korrektur desselben Dokuments bekäme so eine neue Nummer. Sender sollte TXA-12 füllen."))
    if not m.author and m.source_system:
        m.author = m.source_system
        doc.findings.append(Finding("HL7_AUTOR_ERSATZ", INFO, "MSH-3",
                                    f"Kein Verfasser in der Nachricht; sendendes System „{m.source_system}“ eingetragen."))
    if status in _NICHT_FINAL:
        doc.findings.append(Finding(
            "HL7_NICHT_FINAL", PRUEFEN, "TXA-17/OBR-25",
            f"Das Dokument ist laut Quelle „{_NICHT_FINAL[status]}“ ({status}). Das ISiK-Profil kennt nur den Status final.",
            "Vorläufige Dokumente im Kommunikationsserver zurückhalten oder bewusst als final ausleiten."))

    # --- Inhalt
    numeric = 0
    for n, obx in enumerate(msg.all("OBX"), 1):
        typ = msg.get("OBX", 2, seg=obx).strip().upper()
        label = msg.get("OBX", 3, 2, seg=obx) or msg.get("OBX", 3, 1, seg=obx)
        val = msg.get("OBX", 5, seg=obx)
        origin = f"OBX {n}"
        if typ == "ED":
            comps = val.split(msg.cs) + [""] * 5
            payload = _base64_payload(comps[4]) if comps[3].strip().lower() in ("base64", "b64", "") else None
            sub = comps[2].strip().lower()
            if payload and payload[:5] == b"%PDF-":
                from .pdftext import pdf_to_lines
                lines, fnd, stats = pdf_to_lines(payload, cfg, where=f"{origin} (PDF)")
                doc.lines += lines
                doc.findings += fnd
                doc.stats.update(stats)
                doc.original, doc.original_type = payload, "application/pdf"
            elif payload and (sub in ("rtf", "plain", "text", "txt") or payload.lstrip()[:5] == b"{\\rtf"):
                t, f1 = decode_bytes(payload, None, origin)
                if t.lstrip().startswith("{\\rtf"):
                    t = rtf_to_text(t)
                t, f2 = clean_text(t, origin)
                doc.findings += f1 + f2
                doc.lines += [Line(x, origin, hint=label) for x in t.split("\n")]
            else:
                doc.findings.append(Finding(
                    "HL7_ANHANG", PRUEFEN, origin,
                    f"Eingebetteter Anhang vom Typ „{comps[1]}/{comps[2]}“ kann nicht gelesen werden.",
                    "Unterstützt sind PDF, RTF und Text, jeweils Base64-kodiert."))
        elif typ == "RP":
            doc.findings.append(Finding(
                "HL7_VERWEIS", PRUEFEN, origin,
                f"OBX verweist auf eine externe Datei („{msg.unescape(val.split(msg.cs)[0])}“). Der Inhalt steht nicht in der Nachricht.",
                "Datei im Kommunikationsserver einbetten (OBX-2 = ED) oder das PDF direkt an Stellwerk geben."))
        elif typ in ("NM", "SN"):
            numeric += 1
            unit = msg.get("OBX", 6, 1, seg=obx)
            ref = msg.get("OBX", 7, seg=obx)
            flag = msg.get("OBX", 8, seg=obx)
            v = msg.unescape(val.replace(msg.cs, " ")).strip()
            txt = f"{label}: {v} {unit}".strip()
            if ref:
                txt += f" (Referenz {msg.unescape(ref)})"
            if flag and flag not in ("N",):
                txt += f" [{flag}]"
            doc.lines.append(Line(txt, origin))
        else:
            for rep in val.split(msg.rs):
                for x in msg.unescape(rep).split("\n"):
                    doc.lines.append(Line(x.rstrip(), origin, hint=label))
    if numeric:
        doc.findings.append(Finding(
            "HL7_NUMERISCH", INFO, "OBX",
            f"{numeric} numerische Ergebnisse als Textzeilen übernommen.",
            "Strukturierte Observation-Ressourcen erzeugt Stellwerk in dieser Version nicht."))
    if not any(ln.text.strip() for ln in doc.lines):
        doc.findings.append(Finding("HL7_LEER", FEHLER, "OBX", "Die Nachricht enthält keinen Dokumenttext.",
                                    "OBX-Segmente der Nachricht prüfen."))
    return doc


def ack(raw: bytes, code: str = "AA", text: str = "") -> bytes:
    """Bestätigung zu einer empfangenen Nachricht bauen (ohne MLLP-Rahmen)."""
    head = raw.lstrip(b"\x0b\r\n ")
    first = re.split(rb"[\r\n]", head, maxsplit=1)[0]
    if not first.startswith(b"MSH") or len(first) < 8:
        now = datetime.now().strftime("%Y%m%d%H%M%S")
        return f"MSH|^~\\&|STELLWERK||||{now}||ACK|{now}|P|2.5\rMSA|AR||Keine HL7-Nachricht\r".encode("ascii")
    fs = first[3:4]
    f = first.split(fs) + [b""] * 20
    now = datetime.now().strftime("%Y%m%d%H%M%S").encode()
    trig = f[8].split(b"^")[1:2]
    msh = [b"MSH", f[1], f[4], f[5], f[2], f[3], now, b"", b"ACK" + (b"^" + trig[0] if trig else b""),
           b"A" + now, f[10] or b"P", f[11] or b"2.5"]
    if f[17]:
        msh += [b""] * 5 + [f[17]]
    msa = [b"MSA", code.encode(), f[9]] + ([text.encode("ascii", "replace")] if text else [])
    return fs.join(msh) + b"\r" + fs.join(msa) + b"\r"
