"""Übergabepaket: was ein Administrator braucht, um eine Schnittstelle im Kommunikationsserver nachzubauen.

Feld-Mapping (SQL -> Patient, Observation): Abfrage, Zuordnung, Regeln und Testfälle. Die Testfälle sind erfunden
und decken die Sonderfälle ab; echte Zeilen kommen nur auf ausdrücklichen Wunsch dazu (dann enthält das Paket
Patientendaten). Was Stellwerk nur vorgeschlagen hat, steht als Vorschlag im Paket und nie im erwarteten Bundle.

Dokumente (HL7, PDF, Text): Lesen und Abschnittszuordnung bleiben bei Stellwerk. Das Paket beschreibt den Aufruf
und enthält die Vorlage samt den Überschriften, die in der Aufgabe ergänzt wurden.
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from datetime import date, datetime
from decimal import Decimal
from importlib import resources
from pathlib import Path
from urllib.parse import unquote

import yaml

from . import __version__, fieldmap
from .model import FEHLER, zahl
from .pipeline import _safe
from .sqlsource import _SEX, public_url
from .workbench import ApiError, Workspace

ECHTE_ZEILEN = 5

# Schreibweisen von Datumswerten: (Muster, strftime, Beschreibung)
_FORMEN = [
    (r"\d{1,2}\.\d{1,2}\.\d{4}[ T]\d{1,2}:\d{2}:\d{2}$", "%d.%m.%Y %H:%M:%S", "TT.MM.JJJJ HH:MM:SS"),
    (r"\d{1,2}\.\d{1,2}\.\d{4}[ T]\d{1,2}:\d{2}$", "%d.%m.%Y %H:%M", "TT.MM.JJJJ HH:MM"),
    (r"\d{1,2}\.\d{1,2}\.\d{4}$", "%d.%m.%Y", "TT.MM.JJJJ"),
    (r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}", "%Y-%m-%d %H:%M:%S", "JJJJ-MM-TT HH:MM:SS"),
    (r"\d{4}-\d{2}-\d{2}$", "%Y-%m-%d", "JJJJ-MM-TT"),
    (r"\d{14}$", "%Y%m%d%H%M%S", "JJJJMMTTHHMMSS"),
    (r"\d{12}$", "%Y%m%d%H%M", "JJJJMMTTHHMM"),
    (r"\d{8}$", "%Y%m%d", "JJJJMMTT"),
]


def _datumsform(werte) -> tuple[str, str]:
    """(strftime-Muster, Beschreibung) nach der Schreibweise des ersten gefüllten Werts. Der Wert selbst wird nicht verwendet."""
    for v in werte:
        if v in (None, ""):
            continue
        if isinstance(v, datetime):
            return "%Y-%m-%d %H:%M:%S", "Datum mit Uhrzeit (Datentyp der Datenbank)"
        if isinstance(v, date):
            return "%Y-%m-%d", "Datum (Datentyp der Datenbank)"
        s = str(v).strip()
        for pat, fmt, text in _FORMEN:
            if re.match(pat, s):
                if "T" in s[:11]:
                    fmt, text = fmt.replace(" ", "T"), text.replace(" ", "T")
                return fmt, text
        return "", "nicht erkannt"
    return "", "keine Werte gelesen"


def _text(v) -> str | None:
    """Zellwert so, wie ihn die Datenbank liefert, als Text."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def synthetic_rows(cols: list[str], regeln: dict, rows: list[tuple]) -> tuple[list[tuple[str, str, tuple]], dict[str, str]]:
    """Erfundene Zeilen für die Testfälle. Aus den echten Zeilen wird nur die Schreibweise abgelesen (Datum, Dezimalzeichen).

    Liefert ([(Kurzname, Beschreibung, Zeile)], {Spalte: Beschreibung der Datumsschreibweise})."""
    idx = {c: i for i, c in enumerate(cols)}
    ziel = {c: (regeln.get(c) or {}).get("ziel", "") for c in cols}
    werte = [c for c in cols if ziel[c] == "obs.wert"]
    codespalte = next((c for c in cols if ziel[c] == "obs.code"), None)
    tabelle = list(((regeln.get(codespalte) or {}).get("codes") or {}).items()) if codespalte else []
    komma = any("," in str(r[idx[c]]) for c in werte for r in rows if r[idx[c]] not in (None, ""))
    dez = (lambda s: s.replace(".", ",")) if komma else (lambda s: s)
    form: dict[str, str] = {}
    schreibweise: dict[str, str] = {}
    for c in cols:
        if ziel[c] in ("patient.birthDate", "obs.zeit"):
            fmt, text = _datumsform(r[idx[c]] for r in rows)
            form[c] = fmt or ("%d.%m.%Y" if ziel[c] == "patient.birthDate" else "%d.%m.%Y %H:%M")
            schreibweise[c] = text

    def einheit(c: str, nr: int = 1) -> str:
        if codespalte:
            fest = (tabelle[(nr - 1) % len(tabelle)][1].get("einheit") or "").strip() if tabelle else ""
            code = (tabelle[(nr - 1) % len(tabelle)][1].get("loinc") or "").strip() if tabelle else ""
            return fest or (sorted(fieldmap._LOINC.get(code, ("", set()))[1]) or ["mg/dl"])[0]
        code = ((regeln.get((regeln.get(c) or {}).get("fuer") or "") or {}).get("loinc") or "").strip()
        erwartet = sorted(fieldmap._LOINC.get(code, ("", set()))[1])
        return erwartet[0] if erwartet else "mg/dl"

    def zeile(nr: int, leer=(), wert=None, geschlecht="W", zeit=datetime(2026, 1, 15, 8, 30), code=None) -> tuple:
        out = []
        for c in cols:
            z = ziel[c]
            if z in leer:
                v = None
            elif z == "patient.id":
                v = f"90000{nr}"
            elif z == "patient.family":
                v = "Testfall"
            elif z == "patient.given":
                v = "Anna Maria"
            elif z == "patient.birthDate":
                v = datetime(1970, 2, 3).strftime(form[c])
            elif z == "patient.gender":
                v = geschlecht
            elif z == "fall.id":
                v = f"99000{nr}"
            elif z == "auftrag.id":
                v = f"T-000{nr}"
            elif z == "obs.zeit":
                v = zeit.strftime(form[c])
            elif z == "obs.code":
                v = code if code is not None else tabelle[(nr - 1) % len(tabelle)][0]
            elif z == "obs.wert":
                v = wert(c) if wert else dez(f"{werte.index(c) + 1}.10")
            elif z == "obs.einheit":
                v = einheit(c, nr)
            else:
                v = "wird nicht übernommen"
            out.append(v)
        return tuple(out)

    erster = werte[0] if werte else None
    faelle = [
        ("01-vollstaendig", "Alle Spalten gefüllt. Messwerte mit Nachkommastellen, die erhalten bleiben müssen.", zeile(1)),
        ("02-leere-angaben", "Vorname, Geburtsdatum, Geschlecht, Fall, Zeitpunkt und Einheit leer; nur der erste Messwert gefüllt (ganze Zahl). "
                             "Leere Angaben fehlen im Bundle, für leere Messwerte entsteht keine Observation.",
         zeile(2, leer=("patient.given", "patient.birthDate", "patient.gender", "fall.id", "obs.zeit", "obs.einheit"),
               wert=lambda c: "97" if c == erster else None)),
        ("03-kein-zahlenwert", "Der erste Messwert ist keine reine Zahl. Er steht unverändert als valueString im Bundle. "
                               "Der Zeitpunkt liegt in der Sommerzeit.",
         zeile(3, wert=lambda c: dez("<0.6") if c == erster else dez(f"{werte.index(c) + 1}.10"), zeit=datetime(2026, 7, 15, 14, 5))),
    ]
    if any(z in ("patient.gender", "patient.given") for z in ziel.values()):
        faelle.append(("04-unbekannte-codes", "Unbekannter Geschlechtscode (wird unknown) und kein Nachname (dann entfällt name ganz).",
                       zeile(4, leer=("patient.family",), geschlecht="Q")))
    faelle += [
        ("05-ohne-patientennummer", "Patientennummer leer: Für diese Zeile entsteht kein Bundle.", zeile(5, leer=("patient.id",))),
        ("06-ohne-messwert", "Alle Messwerte leer: Für diese Zeile entsteht kein Bundle.", zeile(6, wert=lambda c: None)),
    ]
    if codespalte:
        faelle.append(("07-code-nicht-in-der-tabelle", "Der Analysecode steht nicht in der Codetabelle: Für diese Zeile entsteht kein Bundle, "
                       "der Code wird gemeldet.", zeile(7, code="NICHT-IN-DER-TABELLE")))
    return faelle, schreibweise


# --------------------------------------------------------------------------
# Vergleich
# --------------------------------------------------------------------------

def load_bundle(text: str | bytes):
    """JSON lesen, Dezimalzahlen mit ihrer Schreibweise behalten (1.10 bleibt 1.10)."""
    return json.loads(text, parse_float=Decimal)


def _kennung(entry: dict) -> str:
    r = entry.get("resource") or {}
    ident = r.get("identifier")
    ident = ident[0] if isinstance(ident, list) and ident else ident if isinstance(ident, dict) else {}
    if ident.get("value"):
        return f"{r.get('resourceType')} {ident['value']}"
    code = (((r.get("code") or {}).get("coding") or [{}])[0]).get("code", "")
    return f"{r.get('resourceType')} {code}".strip()


def _normal(bundle: dict) -> dict[str, dict]:
    """Einträge nach Kennung; fullUrl und Verweise darauf durch die Kennung ersetzt, URLs dekodiert."""
    namen: dict[str, str] = {}
    keys: list[str] = []
    for e in bundle.get("entry") or []:
        k, n = _kennung(e), 2
        while k in keys:
            k, n = f"{_kennung(e)} ({n})", n + 1
        keys.append(k)
        if e.get("fullUrl"):
            namen[e["fullUrl"]] = "→ " + k

    def walk(v):
        if isinstance(v, dict):
            return {a: walk(b) for a, b in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return namen.get(v, v) if isinstance(v, str) else v

    out = {}
    for k, e in zip(keys, bundle.get("entry") or []):
        e = walk(e)
        req = e.get("request")
        if isinstance(req, dict):
            e["request"] = {a: unquote(b) if isinstance(b, str) else b for a, b in req.items()}
        out[k] = e
    return out


_ZEIT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})")


def _gleicher_zeitpunkt(a: str, b: str) -> bool:
    """2026-01-15T08:30:00+01:00 und 2026-01-15T07:30:00Z meinen denselben Augenblick."""
    if not (_ZEIT.fullmatch(a) and _ZEIT.fullmatch(b)):
        return False
    try:
        return datetime.fromisoformat(a.replace("Z", "+00:00")) == datetime.fromisoformat(b.replace("Z", "+00:00"))
    except ValueError:
        return False


def compare(soll, ist) -> list[str]:
    """Abweichungen des gelieferten Bundles vom erwarteten, als lesbare Zeilen. Leere Liste = gleich.

    Die Reihenfolge der Einträge und die Werte der fullUrl dürfen abweichen, solange die Verweise stimmen.
    Zeitpunkte gelten als gleich, wenn sie denselben Augenblick meinen (anderer Versatz, gleiche Zeit)."""
    if soll is None:
        return [] if not ist else ["Für diese Zeile darf kein Bundle entstehen, geliefert wurde eins."]
    if not isinstance(ist, dict):
        return ["Erwartet wird ein Bundle, geliefert wurde keins."]
    out: list[str] = []

    def diff(pfad: str, a, b) -> None:
        if isinstance(a, dict) and isinstance(b, dict):
            for k in list(a) + [k for k in b if k not in a]:
                if k not in b:
                    out.append(f"{pfad}.{k}: fehlt (erwartet {json.dumps(a[k], ensure_ascii=False, default=str)[:120]})")
                elif k not in a:
                    out.append(f"{pfad}.{k}: zusätzlich geliefert ({json.dumps(b[k], ensure_ascii=False, default=str)[:120]})")
                else:
                    diff(f"{pfad}.{k}", a[k], b[k])
        elif isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                out.append(f"{pfad}: erwartet {len(a)} Einträge, geliefert {len(b)}")
            for i, (x, y) in enumerate(zip(a, b)):
                diff(f"{pfad}[{i}]", x, y)
        elif isinstance(a, (int, Decimal)) and isinstance(b, (int, Decimal)) and not isinstance(a, bool) and not isinstance(b, bool):
            if str(a) != str(b):
                out.append(f"{pfad}: erwartet {a}, geliefert {b}"
                           + (" (gleicher Zahlenwert, aber andere Nachkommastellen)" if a == b else ""))
        elif isinstance(a, str) and isinstance(b, str) and a != b and _gleicher_zeitpunkt(a, b):
            return
        elif type(a) is not type(b) or a != b:
            out.append(f"{pfad}: erwartet {json.dumps(a, ensure_ascii=False, default=str)}, geliefert {json.dumps(b, ensure_ascii=False, default=str)}")

    for k in ("resourceType", "type"):
        if soll.get(k) != ist.get(k):
            out.append(f"Bundle.{k}: erwartet {soll.get(k)}, geliefert {ist.get(k)}")
    a, b = _normal(soll), _normal(ist)
    for k in a:
        if k not in b:
            out.append(f"{k}: fehlt im gelieferten Bundle")
        else:
            diff(k, {x: y for x, y in a[k].items() if x != "fullUrl"}, {x: y for x, y in b[k].items() if x != "fullUrl"})
    out += [f"{k}: zusätzlich geliefert" for k in b if k not in a]
    return out


# --------------------------------------------------------------------------
# Paket
# --------------------------------------------------------------------------

def task_name(ws: Workspace, tid: str) -> str:
    def find(nodes):
        for n in nodes:
            if n["id"] == tid:
                return n["name"]
            hit = find(n.get("kinder") or [])
            if hit:
                return hit
        return ""
    return find(ws.tree()["kinder"]) or tid


def _json(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


def _zelle(text: str) -> str:
    """Schutz vor Formeln, falls die Tabelle in einer Tabellenkalkulation geöffnet wird."""
    return "'" + text if text[:1] in "=+-@" else text


def _ohne_werte(meldung: str) -> str:
    """Beispielwerte aus einer Meldung nehmen. Einheiten und Codes mit ihrer Häufigkeit bleiben stehen."""
    return re.sub(r" \(z\. B\. .*\)\. Sie stehen", ". Sie stehen", meldung, flags=re.S)


def _felder(ws: Workspace, t: dict, cfg: dict, name: str, mit_daten: bool) -> dict[str, bytes]:
    cols, rows = ws._query(t)
    fd = t["felder"]
    regeln = {c: r for c, r in fd["regeln"].items() if c in cols and r.get("ziel")}
    bundles, findings, _ = fieldmap.build(cols, rows, regeln, cfg, fd["status"])
    fehler = [x for x in findings if x.severity == FEHLER]
    if fehler:
        raise ApiError(409, "Die Zuordnung hat noch Fehler; so lässt sie sich nicht übergeben. " + fehler[0].message)
    vor = fieldmap.suggest(cols, regeln, fd["abgelehnt"])
    faelle, schreibweise = synthetic_rows(cols, regeln, rows)
    sb, _, _ = fieldmap.build(cols, [z for _, _, z in faelle], regeln, cfg, fd["status"])
    muster = sb[0]
    if not muster:
        raise ApiError(409, "Aus der Zuordnung entsteht kein Bundle. Patientennummer und mindestens einen Messwert zuordnen.")
    pat = muster["entry"][0]["resource"]
    obs = muster["entry"][1]["resource"]
    ns = {"patient": pat["identifier"][0]["system"]}
    if "encounter" in obs:
        ns["fall"] = obs["encounter"]["identifier"]["system"]
    if "identifier" in obs:
        ns["auftrag"] = obs["identifier"][0]["system"]
    tz = cfg.get("zeitzone", "Europe/Berlin")
    einheit_von = {r.get("fuer"): c for c, r in regeln.items() if r["ziel"] == "obs.einheit" and r.get("fuer")}
    codespalte = next((c for c, r in regeln.items() if r["ziel"] == "obs.code"), None)
    if codespalte:                                       # Zeilenform: eine Einheitenspalte gilt für die eine Wertspalte
        ecol = next((c for c, r in regeln.items() if r["ziel"] == "obs.einheit"), "")
        einheit_von = {c: ecol for c, r in regeln.items() if r["ziel"] == "obs.wert" and ecol}
    offen_punkte = [{"code": x.code, "wo": x.where, "meldung": x.message if mit_daten else _ohne_werte(x.message), "was_tun": x.action}
                    for x in findings if x.severity == "pruefen"]
    status = "pruefen" if offen_punkte or vor else "ok"

    # --- Zuordnung je Spalte
    spalten = []
    for c in cols:
        r = regeln.get(c)
        e = {"spalte": c, "stand": "", "ziel": "", "pfad": "", "loinc": "", "anzeige": "", "einheit": "", "einheit_spalte": "",
             "einheit_fest": "", "schreibweise": schreibweise.get(c, ""), "hinweis": ""}
        if r:
            e.update(ziel=r["ziel"], pfad=fieldmap.PFAD[r["ziel"]], stand="nicht übernehmen" if r["ziel"] == "ignorieren" else "zugeordnet")
            if r["ziel"] == "obs.wert":
                code = (r.get("loinc") or "").strip()
                fest = (r.get("einheit") or "").strip()
                e.update(loinc=code, anzeige=r.get("anzeige") or fieldmap._LOINC.get(code, ("",))[0],
                         einheit_spalte=einheit_von.get(c, ""), einheit_fest=fest,
                         einheit=" sonst ".join(x for x in (f"aus Spalte {einheit_von[c]}" if c in einheit_von else "", f"fest: {fest}" if fest else "") if x) or "keine")
            if r["ziel"] == "obs.code":
                e.update(codes=[{"code": k, "loinc": (v.get("loinc") or "").strip(),
                                 "anzeige": v.get("anzeige") or fieldmap._LOINC.get((v.get("loinc") or "").strip(), ("",))[0],
                                 "einheit_fest": (v.get("einheit") or "").strip()} for k, v in sorted((r.get("codes") or {}).items())],
                         hinweis=f"Codetabelle mit {len(r.get('codes') or {})} Einträgen (siehe unten und zuordnung.json)")
            if r["ziel"] == "obs.einheit":
                e["hinweis"] = (f"Einheit zu {r.get('fuer')}" if r.get("fuer") else "Einheit je Zeile" if codespalte
                                else "keiner Wertspalte zugewiesen, wird nicht verwendet")
            if c in schreibweise:
                e["hinweis"] = f"Schreibweise in der Quelle: {schreibweise[c]}"
        elif c in vor:
            v = vor[c]
            e.update(stand="Vorschlag, nicht angenommen", pfad=fieldmap.PFAD[v["ziel"]], loinc=v.get("loinc", ""), anzeige=v.get("anzeige", ""),
                     hinweis=v.get("begruendung", ""))
        else:
            e.update(stand="offen", hinweis="Vorschlag wurde abgelehnt" if c in fd["abgelehnt"] else "")
        spalten.append(e)

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(["Spalte", "Stand", "FHIR-Pfad", "LOINC", "Anzeige", "Einheit", "Hinweis"])
    for e in spalten:
        w.writerow([_zelle(e["spalte"]), e["stand"], e["pfad"], e["loinc"], e["anzeige"], e["einheit"], e["hinweis"]])

    jetzt = datetime.now().astimezone().replace(microsecond=0)
    files: dict[str, bytes] = {
        "abfrage.sql": ("\n".join([
            f"-- Schnittstelle: {name}", f"-- Quelle: {public_url(t['db'])}",
            f"-- Aus Stellwerk {__version__}, {jetzt:%d.%m.%Y %H:%M}. Nur lesend ausführen.",
            "-- Beim Entwurf hat Stellwerk höchstens 500 Zeilen gelesen. Für den Betrieb ein Kriterium für neue Zeilen ergänzen.",
            t["abfrage"].strip().rstrip(";") + ";", ""])).encode("utf-8"),
        "zuordnung.csv": ("﻿" + buf.getvalue()).encode("utf-8"),
        "zuordnung.json": _json({
            "aufgabe": name, "erzeugt": jetzt.isoformat(), "stellwerk": __version__, "status": status,
            "quelle": {"datenbank": public_url(t["db"]), "spalten": cols},
            "ziel": {"bundle": "transaction", "observation_status": obs["status"], "zeitzone": tz, "namensraeume": ns},
            "spalten": [{k: v for k, v in e.items() if k != "einheit"} for e in spalten], "geschlecht_codes": _SEX, "offene_punkte": offen_punkte}),
    }
    for slug, _, z in faelle:
        b = sb[[f[0] for f in faelle].index(slug)]
        files[f"testfaelle/{slug}/zeile.json"] = _json(dict(zip(cols, map(_text, z))))
        files[f"testfaelle/{slug}/erwartet.json"] = (fieldmap.dump_json(b, indent=1) + "\n").encode("utf-8")
    echte = 0
    if mit_daten:
        for i, (z, b) in enumerate(zip(rows, bundles), 1):
            if echte == ECHTE_ZEILEN:
                break
            files[f"echte-zeilen/zeile-{i:03d}/zeile.json"] = _json(dict(zip(cols, map(_text, z))))
            files[f"echte-zeilen/zeile-{i:03d}/erwartet.json"] = (fieldmap.dump_json(b, indent=1) + "\n").encode("utf-8")
            echte += 1

    # --- Anleitung
    one = lambda z: next((c for c, r in regeln.items() if r["ziel"] == z), None)       # noqa: E731
    eins = dict(zip(cols, faelle[0][2]))
    drei = dict(zip(cols, faelle[2][2]))
    R = ["Je Zeile der Abfrage entsteht höchstens ein Bundle vom Typ `transaction`. Kein Bundle entsteht, wenn die Patientennummer leer ist "
         "oder kein einziger Messwert gefüllt ist.",
         "Werte und Einheiten werden nie umgerechnet, gerundet oder zerlegt.",
         f"Patient: `identifier` mit `system` `{ns['patient']}`, `type` `MR` (`{fieldmap.V2_0203}`) und der Patientennummer aus `{one('patient.id')}`. "
         "Anfrage: `POST Patient` mit `ifNoneExist: identifier=<system>|<Patientennummer>` (URL-kodiert). Das CDR legt den Patienten also nur an, "
         "wenn es ihn nicht kennt; vorhandene Stammdaten werden nicht überschrieben."]
    if one("patient.family"):
        R.append(f"`name` nur, wenn der Nachname (`{one('patient.family')}`) gefüllt ist, mit `use: official`."
                 + (f" Vornamen (`{one('patient.given')}`) an Leerzeichen trennen: `Anna Maria` wird `[\"Anna\", \"Maria\"]`." if one("patient.given") else ""))
    if one("patient.gender"):
        R.append(f"Geschlecht (`{one('patient.gender')}`) ohne Rücksicht auf Groß- und Kleinschreibung übersetzen: "
                 + ", ".join(f"`{k}` → `{v}`" for k, v in _SEX.items()) + ". Jeder andere Wert wird `unknown`; leer: Element weglassen.")
    if one("patient.birthDate"):
        c = one("patient.birthDate")
        R.append(f"Geburtsdatum (`{c}`, in der Quelle {schreibweise[c]}) als `JJJJ-MM-TT`: `{eins[c]}` wird `{pat.get('birthDate', '')}`. Leer: Element weglassen.")
    if codespalte:
        wc = next(c for c, r in regeln.items() if r["ziel"] == "obs.wert")
        R.append(f"Zeilenform: Jede Zeile enthält ein Ergebnis. Den Analysecode aus `{codespalte}` in der Codetabelle (unten und in `zuordnung.json`) "
                 "nachschlagen, genau und mit Beachtung der Groß- und Kleinschreibung. Steht er dort, entsteht aus dem Wert in "
                 f"`{wc}` eine Observation mit `status` `{obs['status']}`, `category` `laboratory` "
                 "(`http://terminology.hl7.org/CodeSystem/observation-category`), `code.coding` mit `system` `http://loinc.org`, dem LOINC-Code der Tabelle und, "
                 "falls die Tabelle eine Anzeige nennt, `display`; dazu `code.text` = Analysecode und `subject.reference` = `fullUrl` des Patienten "
                 "im selben Bundle. Steht der Code nicht in der Tabelle oder ist er leer, oder ist der Wert leer: keine Observation, und der Code "
                 "wird gemeldet (nie ein LOINC-Code geraten). Leer heißt überall: kein Wert oder nur Leerzeichen; Leerzeichen am Rand werden entfernt.")
    else:
      R.append(f"Je gefüllter Messwert-Spalte eine Observation mit `status` `{obs['status']}`, `category` `laboratory` "
             "(`http://terminology.hl7.org/CodeSystem/observation-category`), `code.coding` mit `system` `http://loinc.org`, dem LOINC-Code und, "
             "falls die Zuordnung eine Anzeige nennt, `display`; dazu `code.text` = Spaltenname und `subject.reference` = `fullUrl` des Patienten "
             "im selben Bundle. Leere Messwert-Spalte: keine Observation. Leer heißt überall: kein Wert oder nur Leerzeichen; "
             "Leerzeichen am Rand werden vor der Übernahme entfernt.")
    R.append("Ist der Wert eine reine Zahl (optional Minus, Ziffern, höchstens ein Komma oder Punkt), steht er als `valueQuantity.value`: "
             "Komma wird Punkt, Nachkommastellen bleiben (`1,10` wird `1.10`, nicht `1.1`). Viele JSON-Bibliotheken, auch JavaScript, schreiben `1.1`; "
             "dann die Zahl als Text in das fertige JSON einsetzen. Jeder andere Wert (`<0,6`, `positiv`) steht unverändert als `valueString`, "
             "gefolgt von Leerzeichen und Einheit, falls es eine gibt; `valueQuantity` entfällt dann.")
    R.append("`valueQuantity.unit` kommt aus der zugeordneten Einheitenspalte, sonst aus der festen Einheit "
             + ("des Analysecodes in der Codetabelle" if codespalte else "der Zuordnung") + ", sonst fehlt es. "
             "Stellwerk setzt kein `system` und keinen `code` (UCUM) und prüft die Einheit nicht gegen UCUM.")
    if one("obs.zeit"):
        c = one("obs.zeit")
        winter = obs.get("effectiveDateTime", "")
        sommer = next((e["resource"].get("effectiveDateTime", "") for e in (sb[2] or {}).get("entry", [])[1:]), "")
        R.append(f"`effectiveDateTime` aus `{c}` (in der Quelle {schreibweise[c]}): ISO 8601 mit dem Versatz der Zeitzone {tz}. "
                 f"`{eins[c]}` wird `{winter}`" + (f", `{drei[c]}` wird `{sommer}` (Sommerzeit)" if sommer else "")
                 + ". Enthält der Wert nur ein Datum, bleibt es ein Datum ohne Uhrzeit. Leer: Element weglassen.")
    if one("fall.id"):
        R.append(f"Fall (`{one('fall.id')}`): `encounter.identifier` mit `system` `{ns['fall']}` und `type` `VN`, kein `reference`. Leer: Element weglassen.")
    if one("auftrag.id"):
        R.append(f"`Observation.identifier.value` = Auftragsnummer (`{one('auftrag.id')}`), Bindestrich, LOINC-Code; `system` `{ns['auftrag']}`. "
                 "Anfrage: `PUT Observation?identifier=<system>|<value>` (URL-kodiert). Ein erneuter Lauf überschreibt die Ergebnisse, statt sie zu verdoppeln.")
    else:
        R.append("Es ist keine Auftragsnummer zugeordnet. Anfrage: `POST Observation`. Ein erneuter Lauf legt die Ergebnisse im CDR doppelt an.")
    R.append("`fullUrl` ist eine `urn:uuid:`. Stellwerk leitet sie aus den Kennungen ab; der Kommunikationsserver darf eigene vergeben, "
             "solange der Verweis auf den Patienten im selben Bundle stimmt.")
    R.append("Das Bundle geht per `POST` an die Basisadresse des FHIR-Servers, `Content-Type: application/fhir+json`.")

    L = [f"# Übergabe: {name}", "",
         f"Aus Stellwerk {__version__}, {jetzt:%d.%m.%Y %H:%M}. Stand der Zuordnung: **{'zu prüfen' if status == 'pruefen' else 'ohne offene Punkte'}**.", "",
         "Dieses Paket beschreibt eine Schnittstelle so, dass sie im Kommunikationsserver (Cloverleaf, BridgeLink, Mirth) nachgebaut werden kann: "
         "mit `abfrage.sql` lesen, je Zeile nach den Regeln unten eine FHIR-Transaktion bauen, an das CDR senden. "
         "Die Testfälle zeigen für jede Regel, was herauskommen muss.", "",
         "## Inhalt", "",
         "| Datei | Zweck |", "|---|---|",
         "| `abfrage.sql` | die Abfrage, wie sie in Stellwerk geprüft wurde |",
         "| `zuordnung.csv` | je Spalte: Ziel im FHIR, LOINC, Einheit, Stand (öffnet sich in Excel) |",
         "| `zuordnung.json` | dasselbe maschinenlesbar, dazu Namensräume, Zeitzone und offene Punkte |",
         "| `testfaelle/` | erfundene Zeilen (`zeile.json`) und das Bundle, das daraus entstehen muss (`erwartet.json`) |"]
    if echte:
        L.append(f"| `echte-zeilen/` | die ersten {echte} Zeilen der Abfrage mit erwartetem Bundle. **Enthält Patientendaten.** |")
    L += ["", "## Quelle", "", f"- Datenbank: `{public_url(t['db'])}` (Passwörter stehen nicht im Paket)",
          f"- Abfrage: `abfrage.sql`, {len(cols)} Spalten",
          "- Für den Betrieb fehlt der Abfrage ein Kriterium für neue Zeilen. Das hängt vom Kommunikationsserver ab (Zeitstempel, laufende Nummer, Merker).",
          "", "## Zuordnung", "", "| Spalte | Stand | FHIR-Pfad | LOINC | Einheit | Hinweis |", "|---|---|---|---|---|---|"]
    L += [f"| `{e['spalte']}` | {e['stand']} | {('`' + e['pfad'] + '`') if e['pfad'] and e['pfad'] != '–' else ''} | {e['loinc']} | {e['einheit']} | {e['hinweis']} |"
          for e in spalten]
    if any(e["stand"].startswith("Vorschlag") for e in spalten):
        L += ["", "Vorschläge sind nicht angenommen und deshalb in keinem erwarteten Bundle enthalten."]
    if codespalte:
        tab = sorted((regeln[codespalte].get("codes") or {}).items())
        L += ["", f"## Codetabelle ({len(tab)} Analysecodes)", "", "| Analysecode | LOINC | Anzeige | feste Einheit |", "|---|---|---|---|"]
        L += [f"| `{k}` | {(v.get('loinc') or '').strip()} | {v.get('anzeige') or fieldmap._LOINC.get((v.get('loinc') or '').strip(), ('',))[0]} | {(v.get('einheit') or '').strip()} |"
              for k, v in tab]
    L += ["", "## Regeln für die Umsetzung", ""] + [f"{i}. {r}" for i, r in enumerate(R, 1)]
    L += ["", "## Testfälle", "", "Jede Zeile in `zeile.json` steht so da, wie die Datenbank sie liefert (alles als Text, leer ist `null`). "
          "`erwartet.json` mit dem Inhalt `null` heißt: Für diese Zeile darf kein Bundle entstehen.", ""]
    L += [f"- `{slug}`: {text}" for slug, text, _ in faelle]
    L += ["", "Vergleichen, was der Kommunikationsserver liefert:", "", "```",
          "stellwerk vergleichen testfaelle/01-vollstaendig/erwartet.json ausgabe-des-servers.json", "```", "",
          "Der Vergleich übergeht die Reihenfolge der Einträge und die Werte der `fullUrl`; Zeitpunkte gelten als gleich, wenn sie denselben Augenblick meinen. "
          "Jede andere Abweichung wird gemeldet, auch verlorene Nachkommastellen."]
    L += ["", "## Offene Punkte", ""]
    L += [f"- **{p['wo']}**: {p['meldung']} {p['was_tun']}".rstrip() for p in offen_punkte] or ["Keine."]
    L += ["", "## Datenschutz", "",
          (f"Der Ordner `echte-zeilen/` enthält Patientendaten aus der Quelle. Das Paket nur dort ablegen und weitergeben, wo das erlaubt ist, "
           "und nach der Abnahme löschen." if echte else
           "Das Paket enthält keine Zeilen und keine einzelnen Werte aus der Quelle. Die Testfälle sind erfunden; aus den echten Daten stammen "
           "nur Spaltennamen, die Schreibweise von Datum und Dezimalzeichen und, unter „Offene Punkte“, Einheiten und Codes mit ihrer Häufigkeit. "
           "Steht in der Abfrage selbst ein Patientenbezug (zum Beispiel eine feste Patientennummer), vor der Weitergabe entfernen."), ""]
    files["LIESMICH.md"] = "\n".join(L).encode("utf-8")
    return files


def _vorlage_yaml(name: str, aliase: dict) -> tuple[str, bytes]:
    p = Path(name)
    if p.suffix in (".yaml", ".yml") and p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    else:
        raw = yaml.safe_load((resources.files("stellwerk") / "vorlagen" / f"{name}.yaml").read_text(encoding="utf-8"))
    n = 0
    for a in raw["abschnitte"]:
        neu = [h for h, k in aliase.items() if k == a["key"] and h not in (a.get("aliase") or [])]
        if neu:
            a["aliase"] = list(a.get("aliase") or []) + neu
            n += len(neu)
    kopf = (f"# Vorlage {raw['name']} aus der Übergabe von Stellwerk {__version__}"
            + (f", ergänzt um {n} Überschrift(en) aus der Aufgabe" if n else "") + ".\n")
    return raw["name"], (kopf + yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, width=110)).encode("utf-8")


def _dokument(ws: Workspace, tid: str, t: dict, cfg: dict, name: str, mit_daten: bool) -> dict[str, bytes]:
    row = ws.store.task(tid)
    if t["art"] == "sql":
        if not t["abfrage"].strip():
            raise ApiError(400, "Die Aufgabe hat noch keine Abfrage.")
    elif not row or row.quelle is None:
        raise ApiError(400, "Die Aufgabe hat noch keine Quelldatei.")
    try:
        vname, vorlage = _vorlage_yaml(t["vorlage"] or cfg.get("vorlage", "entlassbrief"), t["aliase"])
    except (FileNotFoundError, OSError, KeyError, TypeError) as exc:
        raise ApiError(400, f"Die Vorlage der Aufgabe lässt sich nicht lesen: {exc}") from exc
    jetzt = datetime.now().astimezone().replace(microsecond=0)
    last = ws.store.runs(1, tid)
    files = {"vorlage.yaml": vorlage}
    L = [f"# Übergabe: {name}", "", f"Aus Stellwerk {__version__}, {jetzt:%d.%m.%Y %H:%M}.", "",
         "Diese Schnittstelle liefert Dokumente (Arztbrief, Befund). Text lesen, Zeichensatz reparieren, Abschnitte zuordnen und das Bundle bauen "
         "bleibt Aufgabe von Stellwerk. Der Kommunikationsserver reicht die Rohdaten unverändert durch und bekommt das Bundle zurück.", "",
         "## Aufruf", "", "```", f"POST http://<stellwerk-host>:8080/v1/convert?vorlage=<Pfad zu vorlage.yaml auf dem Stellwerk-Host>",
         "Body: die HL7-Nachricht, das PDF oder der Text, unverändert", "",
         '200  {"status": "ok" | "pruefen", "dokumente": [{"bundle": {...}, "befunde": [...]}]}',
         '422  {"status": "fehler", ...}     kein zustellbares Bundle', "```", "",
         "- `ok`: Bundle weiterleiten. `pruefen`: Bundle ist gebaut, ein Mensch soll die Befunde ansehen; im Kommunikationsserver in eine Prüfablage leiten.",
         "- Für PDF und Text ohne Kopfdaten `patient_id`, `fall` und `dokument_id` als Parameter mitgeben.",
         "- Statt per HTTP kann der Kommunikationsserver HL7 auch per MLLP senden (`stellwerk mllp`) und das Bundle aus einem Ordner abholen.",
         "- Der Dienst hat noch keine Anmeldung. Nur im geschützten Netz betreiben.", "",
         "## Vorlage", "", f"`vorlage.yaml` ist die Vorlage `{vname}`"
         + (f", ergänzt um {zahl(len(t['aliase']), 'Überschrift', 'Überschriften')} aus dieser Aufgabe" if t["aliase"] else "")
         + ". Sie gehört auf den Stellwerk-Host; ihr Pfad steht im Aufruf.", ""]
    if t["art"] == "sql":
        files["abfrage.sql"] = "\n".join([f"-- Schnittstelle: {name}", f"-- Quelle: {public_url(t['db'])}",
                                          f"-- Aus Stellwerk {__version__}, {jetzt:%d.%m.%Y %H:%M}. Nur lesend ausführen.",
                                          t["abfrage"].strip().rstrip(";") + ";", ""]).encode("utf-8")
        L += ["## Abfrage", "", f"`abfrage.sql` liest die Dokumente aus `{public_url(t['db'])}`. Stellwerk führt sie selbst aus "
              "(`stellwerk sql`); der Kommunikationsserver kann sie auch ausführen und jede Zeile als Text an den Aufruf oben geben.", ""]
    if t["meta"]:
        L += ["## Feste Kopfdaten", "", "In der Aufgabe von Hand gesetzt und beim Aufruf mitzugeben:", ""]
        L += [f"- `{k}`: `{v}`" for k, v in t["meta"].items()] + [""]
    L += ["## Stand", "", (f"Letzter Lauf in der Workbench: {last[0]['status']}"
                          + (f" (Befunde: {', '.join(last[0]['codes'])})" if last[0].get("codes") else "") + "."
                          if last else "Die Aufgabe wurde noch nicht ausgeführt."), ""]
    if mit_daten:
        if row and row.quelle is not None and t["art"] != "sql":
            files[f"quelle/{_safe(row.quelle_name or 'quelle')}"] = row.quelle
        if row and row.bundle:
            files["bundle.json"] = row.bundle
    L += ["## Datenschutz", "",
          ("Das Paket enthält die Quelldatei und das Bundle des letzten Laufs und damit Patientendaten. Nur dort ablegen und weitergeben, wo das erlaubt ist."
           if mit_daten else "Das Paket enthält weder Quelldaten noch ein Bundle."
           + (" Die festen Kopfdaten oben können einen Patientenbezug haben; vor der Weitergabe prüfen." if t["meta"] else "")), ""]
    files["LIESMICH.md"] = "\n".join(L).encode("utf-8")
    return files


def build_package(ws: Workspace, tid: str, cfg: dict, mit_daten: bool = False) -> tuple[str, bytes]:
    """Paket als ZIP. Liefert (Dateiname, Inhalt)."""
    t = ws.task(tid)
    name = re.sub(r"\s+", " ", task_name(ws, tid)).strip()
    if t["art"] == "sql" and t["modus"] == "felder":
        files = _felder(ws, t, cfg, name, mit_daten)
    else:
        files = _dokument(ws, tid, t, cfg, name, mit_daten)
    root = "uebergabe-" + _safe(name)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(files, key=lambda p: (p != "LIESMICH.md", p)):
            z.writestr(f"{root}/{path}", files[path])
    return root + ("-mit-patientendaten" if mit_daten else "") + ".zip", buf.getvalue()
