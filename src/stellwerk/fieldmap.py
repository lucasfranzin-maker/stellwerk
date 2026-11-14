"""Feld-Mapping: Spalten einer Abfrage auf Patient und Observation abbilden.

Grundsatz: Werte werden nie umgerechnet. Zahl und Einheit kommen so ins Bundle, wie die Quelle
sie liefert (nur die Schreibweise der Zahl wird JSON-tauglich: 1,10 -> 1.10, Nachkommastellen bleiben).
Was nicht eindeutig ist, wird gemeldet und nicht geraten.
"""
from __future__ import annotations

import json
import re
import uuid
from urllib.parse import quote

from .config import PLATZHALTER
from .model import FEHLER, INFO, PRUEFEN, Finding, zahl
from .sqlsource import _FELDER, _SEX, _date

_NS = uuid.UUID("6f1c2f0e-6c3b-4f0e-9a51-5d8c1f3b7a10")
V2_0203 = "http://terminology.hl7.org/CodeSystem/v2-0203"

# Zielfelder, die die Workbench anbietet: (Schlüssel, FHIR-Pfad, Beschreibung)
ZIELE = [
    ("patient.id", "Patient.identifier", "Patientennummer"),
    ("patient.family", "Patient.name.family", "Nachname"),
    ("patient.given", "Patient.name.given", "Vorname"),
    ("patient.birthDate", "Patient.birthDate", "Geburtsdatum"),
    ("patient.gender", "Patient.gender", "Geschlecht"),
    ("fall.id", "Observation.encounter.identifier", "Fallnummer"),
    ("auftrag.id", "Observation.identifier", "Auftrags- oder Befundnummer"),
    ("obs.zeit", "Observation.effectiveDateTime", "Zeitpunkt der Probe oder Messung"),
    ("obs.wert", "Observation.valueQuantity.value", "Messwert (braucht einen LOINC-Code)"),
    ("obs.einheit", "Observation.valueQuantity.unit", "Einheit zu einem Messwert"),
    ("ignorieren", "–", "bewusst nicht übernehmen"),
]
PFAD = {k: p for k, p, _ in ZIELE}

# Vorschlagsliste für Laborspalten: Namensteil -> (LOINC, Anzeige, erwartete Einheiten in Kleinschreibung)
# Vorschläge werden nie automatisch übernommen. Die Einheiten dienen nur der Plausibilitätsmeldung.
LOINC_VORSCHLAG = [
    (("krea",), "2160-0", "Creatinine [Mass/volume] in Serum or Plasma", {"mg/dl"}),
    (("kalium",), "2823-3", "Potassium [Moles/volume] in Serum or Plasma", {"mmol/l"}),
    (("natrium",), "2951-2", "Sodium [Moles/volume] in Serum or Plasma", {"mmol/l"}),
    (("crp",), "1988-5", "C reactive protein [Mass/volume] in Serum or Plasma", {"mg/l"}),
    (("hba1c",), "4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood", {"%"}),
    (("haemoglobin", "hämoglobin", "hb_", "hb"), "718-7", "Hemoglobin [Mass/volume] in Blood", {"g/dl"}),
    (("leuko",), "6690-2", "Leukocytes [#/volume] in Blood by Automated count", set()),
    (("thrombo",), "777-3", "Platelets [#/volume] in Blood by Automated count", set()),
    (("glukose", "glucose"), "2345-7", "Glucose [Mass/volume] in Serum or Plasma", {"mg/dl"}),
]
_LOINC = {code: (anzeige, einheiten) for _, code, anzeige, einheiten in LOINC_VORSCHLAG}
_ZAHL = re.compile(r"^-?\d+(?:[.,]\d+)?$")
_DEZ = re.compile(r'"@@dez:(-?\d+(?:\.\d+)?)@@"')


def dump_json(obj, **kw) -> str:
    """Wie json.dumps, schreibt Messwerte aber mit ihren Nachkommastellen (1.10 bleibt 1.10)."""
    return _DEZ.sub(r"\1", json.dumps(obj, ensure_ascii=False, **kw))


def _stem(col: str) -> str:
    return re.sub(r"(_?(wert|value|val|einheit|unit|eh))$", "", col.strip().lower())


def suggest(columns: list[str], rules: dict, rejected: list[str], llm=None, samples: dict | None = None) -> dict[str, dict]:
    """Vorschläge für noch nicht zugeordnete Spalten. Erst nach Namen, dann optional vom lokalen Modell."""
    out: dict[str, dict] = {}
    offen = [c for c in columns if c not in rules and c not in rejected]
    for col in offen:
        low = col.strip().lower()
        feld = next((f for f, al in _FELDER.items() if low in al), None)
        ziel = {"patient_id": "patient.id", "fallnummer": "fall.id", "name": "patient.family", "vorname": "patient.given",
                "geburtsdatum": "patient.birthDate", "geschlecht": "patient.gender"}.get(feld or "")
        if ziel:
            out[col] = {"ziel": ziel, "quelle": "name", "begruendung": f"Spaltenname „{col}“ ist eine übliche Bezeichnung für {PFAD[ziel]}."}
            continue
        if re.search(r"(auftrag|befund|order)_?(nr|id|nummer)$", low):
            out[col] = {"ziel": "auftrag.id", "quelle": "name", "begruendung": f"Spaltenname „{col}“ sieht nach einer Auftragsnummer aus."}
            continue
        if re.search(r"(abnahme|probe|mess|entnahme).*(zeit|datum|dat|ts)$", low):
            out[col] = {"ziel": "obs.zeit", "quelle": "name", "begruendung": f"Spaltenname „{col}“ sieht nach dem Abnahmezeitpunkt aus."}
            continue
        if re.search(r"(_einheit|_unit|_eh)$", low):
            partner = next((c for c in columns if c != col and _stem(c) == _stem(col)), None)
            if partner:
                out[col] = {"ziel": "obs.einheit", "fuer": partner, "quelle": "name",
                            "begruendung": f"Gleicher Namensstamm wie die Wertspalte „{partner}“."}
            continue
        hit = next((x for x in LOINC_VORSCHLAG if any(low == t or low.startswith(t) for t in x[0])), None)
        if hit:
            out[col] = {"ziel": "obs.wert", "loinc": hit[1], "anzeige": hit[2], "quelle": "name",
                        "begruendung": f"Spaltenname „{col}“ → LOINC {hit[1]} ({hit[2]}). "
                                       + (f"Der Code gilt für die Einheit {', '.join(sorted(hit[3]))}. " if hit[3] else "")
                                       + "Wert und Einheit werden unverändert übernommen."}
    rest = [c for c in offen if c not in out]
    if llm and rest:
        labels = [{"key": k, "titel": p, "beschreibung": d} for k, p, d in ZIELE if k not in ("obs.wert", "obs.einheit")]
        labels += [{"key": f"loinc:{code}", "titel": f"Messwert LOINC {code}", "beschreibung": anz} for _, code, anz, _ in LOINC_VORSCHLAG]
        items = [(i, f"Spalte: {c}\nBeispielwerte: {', '.join((samples or {}).get(c, [])[:3]) or 'keine'}") for i, c in enumerate(rest)]
        try:
            got = llm.classify(items, labels, "ignorieren")
        except Exception:
            got = {}
        for i, (key, conf) in got.items():
            if key == "ignorieren":
                continue
            col = rest[i]
            if key.startswith("loinc:"):
                code = key[6:]
                out[col] = {"ziel": "obs.wert", "loinc": code, "anzeige": _LOINC[code][0]}
            else:
                out[col] = {"ziel": key}
            out[col].update(quelle="modell", sicherheit=conf,
                            begruendung="Vorschlag des lokalen Modells aus Spaltenname und Beispielwerten. Nicht geprüft.")
    return out


def _uid(*parts: str) -> str:
    return str(uuid.uuid5(_NS, "|".join(parts)))


def _ident(system: str, value: str) -> str:
    return "identifier=" + quote(f"{system}|{value}", safe="")


def build(columns: list[str], rows: list[tuple], rules: dict, cfg: dict, status: str = "") -> tuple[list[dict | None], list[Finding], list[str]]:
    """Baut je Zeile eine Transaktion. Liefert (Bundles, Befunde über alle Zeilen, Zeilenbezeichnungen)."""
    f: list[Finding] = []
    idx = {c: i for i, c in enumerate(columns)}
    rules = {c: r for c, r in rules.items() if c in idx and r.get("ziel")}
    by = lambda ziel: [c for c, r in rules.items() if r["ziel"] == ziel]     # noqa: E731
    one = lambda ziel: (by(ziel) or [None])[0]                               # noqa: E731
    tz = cfg.get("zeitzone", "Europe/Berlin")

    sys = dict(cfg.get("systeme") or {})
    used = ["patient"] + (["fall"] if one("fall.id") else []) + (["auftrag"] if one("auftrag.id") else [])
    unset = [k for k in used if not sys.get(k)]
    for k in unset:
        sys[k] = PLATZHALTER.get(k, "https://example.org/fhir/sid/auftragsnummer")
    if unset:
        f.append(Finding("CFG_SYSTEME", PRUEFEN, "stellwerk.yaml",
                         f"Für {', '.join(unset)} ist kein Namensraum eingestellt. Im Bundle stehen Platzhalter (example.org).",
                         "In stellwerk.yaml unter systeme eintragen, sonst findet das Zielsystem Patient und Fall nicht."))

    werte = by("obs.wert")
    if not one("patient.id"):
        f.append(Finding("MAP_PATIENT", FEHLER, "Zuordnung", "Keine Spalte ist als Patientennummer zugeordnet.",
                         "Der Spalte mit der Patientennummer das Ziel Patient.identifier geben."))
    if not werte:
        f.append(Finding("MAP_KEIN_WERT", FEHLER, "Zuordnung", "Keine Spalte ist als Messwert zugeordnet.",
                         "Mindestens einer Spalte das Ziel Observation.valueQuantity.value und einen LOINC-Code geben."))
    for c in werte:
        code = (rules[c].get("loinc") or "").strip()
        if not re.match(r"^\d{1,7}-\d$", code):
            f.append(Finding("MAP_LOINC", FEHLER, c, f"Der Messwert „{c}“ hat keinen gültig geformten LOINC-Code (erwartet z. B. 2160-0).",
                             "LOINC-Code in der Zeile der Spalte eintragen."))
    offen = [c for c in columns if c not in rules]
    if offen:
        f.append(Finding("MAP_OFFEN", PRUEFEN, "Zuordnung",
                         f"{zahl(len(offen), 'Spalte ist', 'Spalten sind')} nicht zugeordnet und fehlen im Bundle: {', '.join(offen)}.",
                         "Zuordnen oder bewusst auf „nicht übernehmen“ stellen."))
    if any(x.severity == FEHLER for x in f):
        return [None] * len(rows), f, [f"Zeile {n}" for n in range(1, len(rows) + 1)]

    if status not in ("final", "preliminary", "amended", "corrected"):
        status = "unknown"
        f.append(Finding("MAP_STATUS", PRUEFEN, "Observation.status",
                         "Der Status der Ergebnisse ist nicht festgelegt; im Bundle steht „unknown“.",
                         "Über der Tabelle festlegen, welchen Status die Ergebnisse dieser Abfrage haben. Stellwerk rät das nicht."))
    if not one("auftrag.id"):
        f.append(Finding("MAP_AUFTRAG", PRUEFEN, "Observation.identifier",
                         "Keine Auftragsnummer zugeordnet. Ein erneuter Lauf legt die Ergebnisse im CDR doppelt an.",
                         "Eine Spalte mit eindeutiger Auftrags- oder Befundnummer zuordnen."))
    if not one("obs.zeit"):
        f.append(Finding("MAP_ZEIT", PRUEFEN, "Observation.effectiveDateTime", "Kein Zeitpunkt zugeordnet; die Ergebnisse haben kein Datum.",
                         "Die Spalte mit Abnahme- oder Messzeit zuordnen."))

    einheit_von = {r.get("fuer"): c for c, r in rules.items() if r["ziel"] == "obs.einheit" and r.get("fuer")}
    text: dict[str, list[str]] = {c: [] for c in werte}
    leer = {c: 0 for c in werte}
    einheiten: dict[str, dict[str, int]] = {c: {} for c in werte}
    sex_unbekannt: dict[str, int] = {}
    datum_kaputt = 0
    bundles: list[dict | None] = []
    labels: list[str] = []

    def cell(row, col):
        v = row[idx[col]] if col else None
        return "" if v is None else str(v).strip()

    for n, row in enumerate(rows, 1):
        pid = cell(row, one("patient.id"))
        auftrag = cell(row, one("auftrag.id"))
        labels.append(f"Zeile {n}" + (f" · {auftrag}" if auftrag else "") + (f" · Patient {pid}" if pid else ""))
        if not pid:
            bundles.append(None)
            continue
        pat_url = "urn:uuid:" + _uid("patient", sys["patient"], pid)
        patient: dict = {"resourceType": "Patient", "identifier": [{
            "type": {"coding": [{"system": V2_0203, "code": "MR"}]}, "system": sys["patient"], "value": pid}]}
        fam, giv = cell(row, one("patient.family")), cell(row, one("patient.given"))
        if fam:
            patient["name"] = [{"use": "official", "family": fam, **({"given": giv.split()} if giv else {})}]
        sex = cell(row, one("patient.gender"))
        if sex:
            patient["gender"] = _SEX.get(sex.lower(), "unknown")
            if sex.lower() not in _SEX:
                sex_unbekannt[sex] = sex_unbekannt.get(sex, 0) + 1
        if one("patient.birthDate") and cell(row, one("patient.birthDate")):
            bd = _date(row[idx[one("patient.birthDate")]], tz)[:10]
            if bd:
                patient["birthDate"] = bd
            else:
                datum_kaputt += 1
        entries = [{"fullUrl": pat_url, "resource": patient,
                    "request": {"method": "POST", "url": "Patient", "ifNoneExist": _ident(sys["patient"], pid)}}]
        zeit = _date(row[idx[one("obs.zeit")]], tz) if one("obs.zeit") and cell(row, one("obs.zeit")) else ""
        if one("obs.zeit") and cell(row, one("obs.zeit")) and not zeit:
            datum_kaputt += 1
        fall = cell(row, one("fall.id"))
        for c in werte:
            raw = cell(row, c)
            if not raw:
                leer[c] += 1
                continue
            r = rules[c]
            code = r["loinc"].strip()
            obs: dict = {"resourceType": "Observation", "status": status,
                         "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category", "code": "laboratory"}]}],
                         "code": {"coding": [{"system": "http://loinc.org", "code": code,
                                              **({"display": r.get("anzeige") or _LOINC.get(code, ("",))[0]} if (r.get("anzeige") or code in _LOINC) else {})}],
                                  "text": c},
                         "subject": {"reference": pat_url}}
            if fall:
                obs["encounter"] = {"identifier": {"type": {"coding": [{"system": V2_0203, "code": "VN"}]}, "system": sys["fall"], "value": fall}}
            if zeit:
                obs["effectiveDateTime"] = zeit
            unit = cell(row, einheit_von.get(c)) or (r.get("einheit") or "").strip()
            if unit:
                einheiten[c][unit] = einheiten[c].get(unit, 0) + 1
            if _ZAHL.match(raw):
                obs["valueQuantity"] = {"value": f"@@dez:{raw.replace(',', '.')}@@", **({"unit": unit} if unit else {})}
            else:
                obs["valueString"] = raw + (f" {unit}" if unit else "")
                if len(text[c]) < 3 and raw not in text[c]:
                    text[c].append(raw)
                text.setdefault("#" + c, []).append(raw)
            if auftrag:
                val = f"{auftrag}-{code}"
                obs["identifier"] = [{"system": sys["auftrag"], "value": val}]
                req = {"method": "PUT", "url": "Observation?" + _ident(sys["auftrag"], val)}
                url = "urn:uuid:" + _uid("obs", sys["auftrag"], val)
            else:
                req = {"method": "POST", "url": "Observation"}
                url = "urn:uuid:" + _uid("obs", pid, code, zeit, raw, str(n))
            entries.append({"fullUrl": url, "resource": obs, "request": req})
        bundles.append({"resourceType": "Bundle", "type": "transaction", "entry": entries} if len(entries) > 1 else None)

    ohne = sum(1 for b in bundles if b is None)
    if ohne:
        f.append(Finding("MAP_ZEILE_LEER", PRUEFEN, "Zeilen",
                         f"{zahl(ohne, 'Zeile ergibt', 'Zeilen ergeben')} kein Bundle (Patientennummer oder alle Messwerte leer).",
                         "Abfrage prüfen oder solche Zeilen dort ausschließen."))
    for c in werte:
        n_text = len(text.get("#" + c, []))
        if n_text:
            f.append(Finding(
                "MAP_KEINE_ZAHL", PRUEFEN, c,
                f"{zahl(n_text, 'Wert ist', 'Werte sind')} keine reine Zahl (z. B. {', '.join('„' + x + '“' for x in text[c])}). "
                "Sie stehen unverändert als Text im Bundle (valueString), nicht als Messgröße.",
                "Mit dem Fachbereich klären, wie solche Werte ins CDR sollen. Stellwerk zerlegt sie nicht."))
        if leer[c]:
            f.append(Finding("MAP_LEER", INFO, c, f"{zahl(leer[c], 'Zeile hat', 'Zeilen haben')} hier keinen Wert; dafür entsteht keine Observation."))
        units = einheiten[c]
        if len(units) > 1:
            f.append(Finding(
                "MAP_EINHEITEN", PRUEFEN, c,
                "Gemischte Einheiten: " + ", ".join(f"{u} ({k}×)" for u, k in sorted(units.items(), key=lambda x: -x[1]))
                + ". Jeder Wert steht mit seiner gelieferten Einheit im Bundle, nichts wird umgerechnet.",
                "Klären, ob ein LOINC-Code für alle Einheiten stimmt. Sonst die Abfrage je Einheit trennen."))
        elif not units:
            f.append(Finding("MAP_OHNE_EINHEIT", PRUEFEN, c, "Zu diesem Messwert gibt es keine Einheit.",
                             "Einheitenspalte zuordnen oder eine feste Einheit eintragen, wenn die Quelle immer dieselbe liefert."))
        erwartet = _LOINC.get(rules[c]["loinc"].strip(), ("", set()))[1]
        fremd = [u for u in units if erwartet and u.lower().replace("μ", "µ") not in erwartet]
        if fremd:
            f.append(Finding(
                "MAP_LOINC_EINHEIT", PRUEFEN, c,
                f"LOINC {rules[c]['loinc']} passt zur Einheit {', '.join(sorted(erwartet))}; geliefert wird {', '.join(fremd)}.",
                "LOINC-Code prüfen. Für andere Einheiten gibt es meist einen eigenen Code."))
    if sex_unbekannt:
        f.append(Finding("MAP_GESCHLECHT", PRUEFEN, one("patient.gender"),
                         "Unbekannte Geschlechtscodes: " + ", ".join(f"„{k}“ ({v}×)" for k, v in sex_unbekannt.items())
                         + ". Im Bundle steht dafür „unknown“.", "Bedeutung der Codes mit dem Quellsystem klären."))
    if datum_kaputt:
        f.append(Finding("MAP_DATUM", PRUEFEN, "Datumsangaben",
                         f"{zahl(datum_kaputt, 'Datumswert ließ', 'Datumswerte ließen')} sich nicht lesen und fehlt/fehlen im Bundle.",
                         "Format der Spalte prüfen (erwartet z. B. 12.11.2026 14:15 oder 2026-11-12)."))
    return bundles, f, labels
