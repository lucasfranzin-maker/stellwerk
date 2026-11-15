"""Dokumente aus einer SQL-Abfrage oder einem CSV-Export lesen.

Drei Formen werden erkannt:
- breit: eine Zeile je Dokument, je Abschnitt eine Spalte
- lang:  mehrere Zeilen je Dokument, Spalten "abschnitt" und "abschnittstext"
- text:  eine Spalte mit dem ganzen Brief (auch RTF oder HTML)
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

from .hl7v2 import ts_to_fhir
from .model import FEHLER, INFO, PRUEFEN, Finding, Line, SourceDoc
from .sections import norm
from .textrepair import clean_text, decode_bytes, strip_markup


class SqlError(Exception):
    pass


_FELDER = {
    "patient_id": ["patient_id", "patientid", "pat_id", "patid", "patnr", "pat_nr", "patientennummer", "pid", "patient"],
    "fallnummer": ["fallnummer", "fallnr", "fall_nr", "fall_id", "fallid", "aufnahmenummer", "aufnahmenr", "fall", "case_id"],
    "fallart": ["fallart", "fall_art", "falltyp", "patientenklasse", "behandlungsart"],
    "dokument_id": ["dokument_id", "dokumentid", "dok_id", "dokid", "doknr", "dok_nr", "dokumentnummer", "brief_id", "briefnr", "doc_id", "id"],
    "datum": ["datum", "dokdatum", "dok_datum", "dokumentdatum", "briefdatum", "erstellt_am", "erstelldatum", "freigabedatum", "created"],
    "autor": ["autor", "verfasser", "arzt", "ersteller", "unterzeichner", "author", "freigeber"],
    "titel": ["titel", "betreff", "bezeichnung", "title", "dokumenttitel"],
    "name": ["name", "nachname", "familienname", "pat_name", "patname"],
    "vorname": ["vorname", "pat_vorname", "patvorname"],
    "geburtsdatum": ["geburtsdatum", "gebdat", "geb_datum", "gebdatum", "geboren", "birthdate"],
    "geschlecht": ["geschlecht", "sex", "gender"],
    "text": ["text", "brieftext", "brief", "inhalt_gesamt", "dokumenttext", "volltext", "rtf", "body"],
    "abschnitt": ["abschnitt", "abschnittsname", "kapitel", "section", "baustein", "feldname"],
    "abschnittstext": ["abschnittstext", "inhalt", "abschnitt_text", "kapiteltext", "bausteintext", "wert", "feldinhalt"],
    "reihenfolge": ["reihenfolge", "pos", "position", "sort", "lfdnr", "lfd_nr"],
}
_SUFFIX = re.compile(r"(_?(txt|text|inhalt|memo|lang))$")
_SCHREIBEND = re.compile(r"\b(insert|update|delete|drop|alter|create|truncate|merge|grant|revoke|exec|execute|call|attach|pragma|replace)\b", re.I)


_PLATZHALTER = re.compile(r"\$\{(STELLWERK_DB_[A-Z0-9_]+)\}")


def resolve_url(db: str) -> str:
    """${STELLWERK_DB_...} in der Adresse durch die Umgebungsvariable ersetzen. So steht kein Passwort in Aufgaben oder Dateien."""
    import os

    at = db.find("@", db.find("://") + 3) if "://" in db else -1

    def sub(m: re.Match) -> str:
        val = os.environ.get(m.group(1))
        if val is None:
            raise SqlError(f"Die Umgebungsvariable {m.group(1)} ist nicht gesetzt.")
        if 0 <= m.start() < at:                                  # Teil der Anmeldedaten: für die URL kodieren
            from urllib.parse import quote
            return quote(val, safe="")
        return val
    return _PLATZHALTER.sub(sub, db)


def public_url(db: str) -> str:
    """Adresse ohne Passwort, für Protokoll und als Schlüssel."""
    m = re.match(r"^([a-z0-9+]+://)([^:/@]+):([^@]*)@(.*)$", db, re.I)
    if m and not _PLATZHALTER.fullmatch(m.group(3)):
        return f"{m.group(1)}{m.group(2)}:***@{m.group(4)}"
    return db


def has_password(db: str) -> bool:
    return public_url(db) != db


def check_readonly(query: str) -> None:
    q = re.sub(r"--[^\n]*|/\*.*?\*/", " ", query, flags=re.S).strip().rstrip(";").strip()
    if ";" in q:
        raise SqlError("Die Abfrage enthält mehrere Anweisungen. Erlaubt ist genau ein SELECT.")
    if not re.match(r"^(select|with)\b", q, re.I) or _SCHREIBEND.search(re.sub(r"'[^']*'", "''", q)):
        raise SqlError("Stellwerk führt nur lesende Abfragen aus (SELECT). Diese Abfrage wurde nicht ausgeführt.")


_DRIVERS = {
    "psycopg": "pip install 'stellwerk[postgres]'",
    "psycopg2": "pip install psycopg2-binary",
    "oracledb": "pip install oracledb",
    "cx_Oracle": "pip install oracledb (Adresse dann mit oracle+oracledb://)",
    "pyodbc": "pip install pyodbc",
    "pymssql": "pip install pymssql",
    "pymysql": "pip install pymysql",
    "MySQLdb": "pip install mysqlclient",
    "mariadb": "pip install mariadb",
}


def driver_missing(exc: ImportError) -> str:
    """SQLAlchemy kennt die Datenbankart, aber das Python-Modul für die Verbindung fehlt. Sagen, was zu installieren ist."""
    name = (getattr(exc, "name", None) or "").split(".")[0]
    if not name:
        return f"Der Treiber für diese Datenbank lässt sich nicht laden: {exc}"
    how = _DRIVERS.get(name, f"pip install {name}")
    return (f"Der Datenbanktreiber „{name}“ ist auf dem Stellwerk-Server nicht installiert. "
            f"Nachinstallieren mit: {how}")


def run_query(db: str, query: str, limit: int | None = None) -> tuple[list[str], list[tuple]]:
    check_readonly(query)
    db = resolve_url(db)
    if db.startswith("sqlite:///"):
        path = Path(db[len("sqlite:///"):])
        if not path.exists():
            raise SqlError(f"Datenbankdatei nicht gefunden: {path}")
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            cur = con.execute(query)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(limit) if limit else cur.fetchall()
        except sqlite3.Error as exc:
            raise SqlError(f"Die Datenbank meldet: {exc}") from exc
        finally:
            con.close()
        return cols, rows
    try:
        import sqlalchemy
    except ImportError as exc:
        raise SqlError("Für andere Datenbanken als SQLite wird SQLAlchemy samt Treiber gebraucht "
                       "(pip install 'stellwerk[sql]' oracledb).") from exc
    try:
        with sqlalchemy.create_engine(db).connect() as con:
            res = con.execute(sqlalchemy.text(query))
            cols = list(res.keys())
            rows = res.fetchmany(limit) if limit else res.fetchall()
    except ImportError as exc:
        raise SqlError(driver_missing(exc)) from exc
    except sqlalchemy.exc.SQLAlchemyError as exc:
        raise SqlError(f"Die Datenbank meldet: {str(exc).splitlines()[0]}") from exc
    return cols, [tuple(r) for r in rows]


def read_csv(data: bytes, where: str = "CSV") -> tuple[list[str], list[tuple], list[Finding]]:
    text, findings = decode_bytes(data, None, where)
    try:
        dialect = csv.Sniffer().sniff(text[:5000], delimiters=";,\t|")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";"
    rows = list(csv.reader(io.StringIO(text, newline=""), dialect))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return [], [], findings
    return [c.strip() for c in rows[0]], [tuple(r) for r in rows[1:]], findings


def guess_columns(columns: list[str], tpl) -> tuple[dict[str, str], dict[str, str]]:
    """Spaltennamen deuten: (Feld -> Spalte, Spalte -> Abschnitt)."""
    felder: dict[str, str] = {}
    abschnitte: dict[str, str] = {}
    for col in columns:
        low = col.strip().lower()
        hit = next((f for f, al in _FELDER.items() if low in al and f not in felder), None)
        if hit:
            felder[hit] = col
            continue
        sec = tpl.lookup(_SUFFIX.sub("", low).replace("_", " "))
        if sec and sec[1] == 1.0:
            abschnitte[col] = sec[0]
    return felder, abschnitte


def _date(v, tz: str) -> str:
    if v is None or v == "":
        return ""
    if isinstance(v, datetime):
        return ts_to_fhir(v.strftime("%Y%m%d%H%M%S"), tz)[0]
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$", s)
    if m:
        d, mo, y, h, mi, sec = m.groups()
        return ts_to_fhir(f"{y}{int(mo):02d}{int(d):02d}" + (f"{int(h):02d}{mi}{sec or '00'}" if h else ""), tz)[0]
    return ts_to_fhir(re.sub(r"[-:T ]", "", s)[:14], tz)[0]


_SEX = {"m": "male", "w": "female", "f": "female", "d": "other", "x": "other", "u": "unknown",
        "männlich": "male", "weiblich": "female", "divers": "other", "male": "male", "female": "female"}
_ART = {"s": "IMP", "stationär": "IMP", "stationaer": "IMP", "i": "IMP", "a": "AMB", "ambulant": "AMB", "o": "AMB",
        "n": "EMER", "notfall": "EMER", "e": "EMER"}


def docs_from_rows(columns: list[str], rows: list[tuple], mapping: dict, tpl, cfg: dict, name: str,
                   pre: list[Finding] | None = None) -> list[SourceDoc]:
    tz = cfg.get("zeitzone", "Europe/Berlin")
    felder, abschnitte = guess_columns(columns, tpl)
    geraten = dict(felder)
    felder.update({k: v for k, v in (mapping.get("spalten") or {}).items() if v})
    abschnitte.update(mapping.get("abschnitte") or {})
    col_fehlt = [c for c in list(felder.values()) + list(abschnitte) if c not in columns]
    idx = {c: i for i, c in enumerate(columns)}
    kopf: list[Finding] = list(pre or [])
    if col_fehlt:
        kopf.append(Finding("SQL_SPALTE_FEHLT", FEHLER, "Zuordnung",
                            f"Zugeordnete Spalten kommen im Ergebnis nicht vor: {', '.join(col_fehlt)}.",
                            f"Vorhanden sind: {', '.join(columns)}."))
        return [SourceDoc("sql", name, findings=kopf)]
    for a in abschnitte.values():
        if a not in tpl.by_key:
            kopf.append(Finding("SQL_ABSCHNITT", FEHLER, "Zuordnung", f"Die Vorlage {tpl.name} hat keinen Abschnitt „{a}“.",
                                f"Möglich sind: {', '.join(tpl.by_key)}."))
            return [SourceDoc("sql", name, findings=kopf)]

    lang = "abschnitt" in felder and "abschnittstext" in felder
    used = set(felder.values()) | set(abschnitte)
    unused = [c for c in columns if c not in used]
    auto = [f"{c} → {f}" for f, c in geraten.items() if (mapping.get("spalten") or {}).get(f) != c]
    auto += [f"{c} → Abschnitt {a}" for c, a in abschnitte.items() if c not in (mapping.get("abschnitte") or {})]
    if auto:
        kopf.append(Finding("SQL_GERATEN", INFO, "Zuordnung",
                            "Spalten nach ihrem Namen zugeordnet: " + "; ".join(auto) + ".",
                            "Stimmt etwas nicht, die Zuordnung in der Datei unter spalten bzw. abschnitte festschreiben."))
    if unused:
        kopf.append(Finding("SQL_UNGENUTZT", PRUEFEN, "Zuordnung",
                            f"Diese Spalten sind keinem Feld und keinem Abschnitt zugeordnet und fehlen im Dokument: {', '.join(unused)}.",
                            "In der Zuordnungsdatei unter abschnitte eintragen oder aus der Abfrage nehmen."))

    def val(row, feld):
        c = felder.get(feld)
        return row[idx[c]] if c else None

    def text_of(v, where: str, findings: list[Finding]) -> str:
        if v is None:
            return ""
        if isinstance(v, (bytes, bytearray, memoryview)):
            v, f = decode_bytes(bytes(v), None, where)
            findings += f
        t, f1 = strip_markup(str(v), where)
        t, f2 = clean_text(t, where)
        findings += f1 + f2
        return t.strip("\n")

    groups: dict[str, list[tuple]] = {}
    for n, row in enumerate(rows, 1):
        key = str(val(row, "dokument_id") or (f"zeile-{n}" if not lang else ""))
        groups.setdefault(key, []).append(row)

    docs: list[SourceDoc] = []
    for doc_key, grp in groups.items():
        doc = SourceDoc("sql", f"{name} · Dokument {doc_key}", findings=list(kopf))
        first = grp[0]
        m = doc.meta
        s = lambda feld: str(val(first, feld) or "").strip()        # noqa: E731
        m.patient_id, m.encounter_id, m.doc_id = s("patient_id"), s("fallnummer"), s("dokument_id")
        m.family, _ = clean_text(s("name"))
        m.given, _ = clean_text(s("vorname"))
        m.author, _ = clean_text(s("autor"))
        m.title, _ = clean_text(s("titel"))
        m.doc_date = _date(val(first, "datum"), tz)
        m.birth_date = _date(val(first, "geburtsdatum"), tz)[:10]
        m.gender = _SEX.get(s("geschlecht").lower(), "unknown" if s("geschlecht") else "")
        m.gender_amtlich = s("geschlecht").upper() if s("geschlecht").upper() in ("D", "X") else ""
        m.encounter_class = _ART.get(s("fallart").lower(), "")
        if "," in m.family and not m.given:
            m.family, m.given = [x.strip() for x in m.family.split(",", 1)]
        if not felder.get("dokument_id"):
            doc.findings.append(Finding("SQL_DOKID", FEHLER, "Zuordnung", "Keine Spalte für die Dokumentnummer erkannt.",
                                        "In der Zuordnungsdatei spalten.dokument_id setzen."))
        if lang:
            order = felder.get("reihenfolge")
            if order:
                grp = sorted(grp, key=lambda r: (r[idx[order]] is None, r[idx[order]]))
            for row in grp:
                where = f"Abschnitt {val(row, 'abschnitt')}"
                t = text_of(val(row, "abschnittstext"), where, doc.findings)
                doc.lines += [Line(x, where, hint=str(val(row, "abschnitt") or "")) for x in t.split("\n") if t]
        else:
            if "text" in felder:
                where = f"Spalte {felder['text']}"
                t = text_of(val(first, "text"), where, doc.findings)
                doc.lines += [Line(x, where) for x in t.split("\n")]
            for col, sec in abschnitte.items():
                where = f"Spalte {col}"
                t = text_of(first[idx[col]], where, doc.findings)
                if t.strip():
                    doc.lines += [Line(x, where, hint=tpl.by_key[sec]["titel"]) for x in t.split("\n")]
            if len(grp) > 1:
                doc.findings.append(Finding(
                    "SQL_MEHRFACH", PRUEFEN, f"Dokument {doc_key}",
                    f"{len(grp)} Zeilen mit derselben Dokumentnummer; nur die erste wurde verwendet.",
                    "Abfrage prüfen (fehlender Join-Schlüssel?) oder die lange Form mit abschnitt/abschnittstext nutzen."))
        if len({ln.hint for ln in doc.lines if ln.hint}) == 1:      # ein einzelner Hinweis ist keine Gliederung
            only = next(ln.hint for ln in doc.lines if ln.hint)
            doc.lines.insert(0, Line(only + ":", doc.lines[0].origin))
            for ln in doc.lines:
                ln.hint = ""
        if not any(ln.text.strip() for ln in doc.lines):
            doc.findings.append(Finding("SQL_LEER", FEHLER, f"Dokument {doc_key}", "Die Zeile enthält keinen Dokumenttext.",
                                        "Prüfen, ob die Textspalten richtig zugeordnet sind."))
        docs.append(doc)
    if not docs:
        docs.append(SourceDoc("sql", name, findings=kopf + [Finding(
            "SQL_KEINE_ZEILEN", FEHLER, "Abfrage", "Die Abfrage liefert keine Zeilen.", "Abfrage und Filter prüfen.")]))
    return docs
