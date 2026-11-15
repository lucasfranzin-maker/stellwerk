"""FHIR-Ausgabe: Composition nach ISiKBerichtSubSysteme im Dokument-Bundle (ISiKBerichtBundle)
oder als Transaktion für ein CDR. Dazu eingebaute Prüfungen der Regeln, die das Profil stellt."""
from __future__ import annotations

import hashlib
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from html import escape
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .config import PLATZHALTER
from .model import FEHLER, INFO, PRUEFEN, Finding, Section, SourceDoc

ISIK_COMPOSITION = "https://gematik.de/fhir/isik/StructureDefinition/ISiKBerichtSubSysteme"
ISIK_BUNDLE = "https://gematik.de/fhir/isik/StructureDefinition/ISiKBerichtBundle"
KDL = "http://dvmd.de/fhir/CodeSystem/kdl"
V2_0203 = "http://terminology.hl7.org/CodeSystem/v2-0203"
_NS = uuid.UUID("6f1c2f0e-6c3b-4f0e-9a51-5d8c1f3b7a10")
XHTML = "http://www.w3.org/1999/xhtml"
_FALLART = {"stationaer": "IMP", "stationär": "IMP", "ambulant": "AMB", "notfall": "EMER",
            "IMP": "IMP", "AMB": "AMB", "EMER": "EMER", "PRENC": "PRENC"}
_GESCHLECHT = {"male": "männlich", "female": "weiblich", "other": "divers", "unknown": "unbekannt"}


def _uid(*parts: str) -> str:
    return str(uuid.uuid5(_NS, "|".join(parts)))


def _datum_de(fhir_dt: str) -> str:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?", fhir_dt or "")
    if not m:
        return fhir_dt or ""
    y, mo, d, h, mi = m.groups()
    return f"{d}.{mo}.{y}" + (f" {h}:{mi}" if h else "")


def _line_html(text: str) -> str:
    text = re.sub(r" {2,}", lambda m: " " * len(m.group(0)), text.replace("\t", "    "))
    return escape(text, quote=False)


def section_div(sec: Section) -> str:
    out = [f'<div xmlns="{XHTML}">']
    many = len(sec.parts) > 1
    for part in sec.parts:
        if part.heading and (many or True):
            out.append(f"<p><b>{escape(part.heading, quote=False)}</b></p>")
        para: list[str] = []
        for ln in part.lines + [None]:
            if ln is None or not ln.text.strip():
                if para:
                    out.append("<p>" + "<br/>".join(para) + "</p>")
                    para = []
            else:
                para.append(_line_html(ln.text))
    out.append("</div>")
    return "".join(out)


def build_bundle(doc: SourceDoc, sections: list[Section], tpl, cfg: dict,
                 now: datetime | None = None) -> tuple[dict | None, list[Finding]]:
    """Baut das Bundle. Fehlen Pflichtangaben, gibt es kein Bundle, sondern Befunde."""
    f: list[Finding] = []
    m = doc.meta
    sys = dict(cfg.get("systeme") or {})
    unset = [k for k in ("patient", "fall", "dokument") if not sys.get(k)]
    if unset:
        f.append(Finding(
            "CFG_SYSTEME", PRUEFEN, "stellwerk.yaml",
            "Für " + ", ".join({"patient": "Patientennummer", "fall": "Fallnummer", "dokument": "Dokumentnummer"}[k] for k in unset)
            + " ist kein Namensraum eingestellt. Im Bundle stehen Platzhalter (example.org).",
            "In stellwerk.yaml unter systeme die Namensräume des Hauses eintragen, sonst findet das Zielsystem Patient und Fall nicht."))
    for k in unset:
        sys[k] = PLATZHALTER[k]

    if not m.patient_id:
        f.append(Finding("META_PATIENT", FEHLER, "Patient", "Die Patientennummer fehlt. Ohne sie gibt es kein gültiges Dokument.",
                         "In der Workbench im Reiter „Kopfdaten“ eintragen, bei Datenbanken die Spalte dort zuordnen. Bei HL7: PID-3 prüfen. "
                         "Auf der Kommandozeile: --patient-id."))
    if not sections:
        f.append(Finding("META_INHALT", FEHLER, "Inhalt", "Aus dem Eingang ließ sich kein Text lesen.",
                         "Bei gescannten PDFs: Scanqualität prüfen (300 dpi, gerade, kontrastreich). Sonst Eingangsdatei prüfen."))
    if not m.doc_id:
        f.append(Finding("META_DOKID", FEHLER, "Dokument", "Die Dokumentnummer fehlt.",
                         "In der Workbench im Reiter „Kopfdaten“ eintragen, bei Datenbanken die Spalte dort zuordnen. Auf der Kommandozeile: --dokument-id."))
    if any(x.severity == FEHLER for x in f):
        return None, f

    tz = ZoneInfo(cfg.get("zeitzone", "Europe/Berlin"))
    now = now or datetime.now(tz).replace(microsecond=0)
    if not m.doc_date:
        m.doc_date = now.isoformat()
        f.append(Finding("META_DATUM", PRUEFEN, "Dokument", "Kein Dokumentdatum in der Quelle; Verarbeitungszeit eingetragen.",
                         "Datum mitliefern (TXA-4, Spalte, --datum)."))
    title = m.title or (f"{tpl.titel} vom {_datum_de(m.doc_date)[:10]}")
    author = m.author or (cfg.get("standard") or {}).get("autor") or ""
    if not author:
        author = "Unbekannt"
        f.append(Finding("META_AUTOR", PRUEFEN, "Dokument", "Kein Verfasser in der Quelle; „Unbekannt“ eingetragen.",
                         "Verfasser mitliefern oder standard.autor in stellwerk.yaml setzen (z. B. Name des Subsystems)."))
    fehlend = [n for n, v in (("Nachname", m.family), ("Geburtsdatum", m.birth_date)) if not v]
    if fehlend:
        f.append(Finding(
            "META_STAMMDATEN", PRUEFEN, "Patient",
            f"{' und '.join(fehlend)} des Patienten fehlen. ISiK verlangt beides im Dokumentkopf; "
            "das Zielsystem kann so nur über die Patientennummer abgleichen.",
            "Stammdaten mitliefern (PID-5, PID-7 oder Spalten name, geburtsdatum)."))

    pat_id = _uid("patient", sys["patient"], m.patient_id)
    comp_id = _uid("composition", sys["dokument"], m.doc_id)
    pat_url, comp_url = f"urn:uuid:{pat_id}", f"urn:uuid:{comp_id}"

    patient: dict = {"resourceType": "Patient", "id": pat_id, "identifier": [{
        "type": {"coding": [{"system": V2_0203, "code": "MR"}]}, "system": sys["patient"], "value": m.patient_id}]}
    if m.family:
        name: dict = {"use": "official", "family": m.family}
        if m.given:
            name["given"] = m.given.split()
        patient["name"] = [name]
    if m.gender:
        patient["gender"] = m.gender
        if m.gender_amtlich:
            patient["_gender"] = {"extension": [{
                "url": "http://fhir.de/StructureDefinition/gender-amtlich-de",
                "valueCoding": {"system": "http://fhir.de/CodeSystem/gender-amtlich-de", "code": m.gender_amtlich}}]}
    if m.birth_date:
        patient["birthDate"] = m.birth_date

    encounter = None
    klasse = m.encounter_class or _FALLART.get(str((cfg.get("standard") or {}).get("fallart") or ""), "")
    if m.encounter_id and klasse:
        enc_id = _uid("encounter", sys["fall"], m.encounter_id)
        encounter = {"resourceType": "Encounter", "id": enc_id, "identifier": [{
            "type": {"coding": [{"system": V2_0203, "code": "VN"}]}, "system": sys["fall"], "value": m.encounter_id}],
            "status": "unknown",
            "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": klasse},
            "subject": {"reference": pat_url}}
    elif m.encounter_id:
        f.append(Finding("META_FALLART", PRUEFEN, "Fall",
                         "Fallnummer vorhanden, aber die Fallart (stationär/ambulant) ist unbekannt. Kein Fallbezug im Bundle.",
                         "Fallart mitliefern (PV1-2, Spalte fallart) oder standard.fallart in stellwerk.yaml setzen."))
    else:
        f.append(Finding("META_FALL", PRUEFEN, "Fall", "Keine Fallnummer in der Quelle. Kein Fallbezug im Bundle.",
                         "Fallnummer mitliefern (PV1-19, Spalte fallnummer, --fall). ISiK erwartet den Fallbezug."))

    rows = [("Patient", ", ".join(x for x in (m.family, m.given) if x)
             + (f", geb. {_datum_de(m.birth_date)}" if m.birth_date else "")
             + (f", {_GESCHLECHT.get(m.gender, m.gender)}" if m.gender else "")),
            ("Patientennummer", m.patient_id), ("Fallnummer", m.encounter_id), ("Dokumenttyp", tpl.titel),
            ("Status", "final"), ("Datum", _datum_de(m.doc_date)), ("Verfasser", author), ("Dokumentnummer", m.doc_id)]
    head = [f'<div xmlns="{XHTML}"><h1>{escape(title, quote=False)}</h1><table>']
    head += [f"<tr><td>{k}</td><td>{escape(v, quote=False)}</td></tr>" for k, v in rows if v]
    head.append("</table>")
    if doc.stats.get("ocr_seiten"):
        head.append(f"<p><i>Hinweis: Der Text wurde per Texterkennung aus einem gescannten Original gewonnen "
                    f"(mittlere Erkennungssicherheit {doc.stats.get('ocr_sicherheit', 0):.0f} %). "
                    "Im Zweifel gilt das Original.</i></p>")
    head.append("</div>")

    typ: dict = {"text": tpl.titel}
    if tpl.kdl:
        typ["coding"] = [{"system": KDL, "code": tpl.kdl, **({"display": tpl.kdl_anzeige} if tpl.kdl_anzeige else {})}]
    else:
        f.append(Finding("CFG_KDL", INFO, tpl.pfad, "Die Vorlage nennt keinen KDL-Code; der Dokumenttyp steht nur als Text im Bundle.",
                         "typ.kdl in der Vorlage eintragen, damit das Zielsystem das Dokument einordnen kann."))
    comp: dict = {
        "resourceType": "Composition", "id": comp_id, "meta": {"profile": [ISIK_COMPOSITION]},
        "text": {"status": "extensions", "div": "".join(head)},
        "identifier": {"system": sys["dokument"], "value": m.doc_id},
        "status": "final", "type": typ, "subject": {"reference": pat_url},
        "date": m.doc_date, "author": [{"display": author}], "title": title, "section": []}
    if encounter:
        comp["encounter"] = {"reference": f"urn:uuid:{encounter['id']}"}
    for sec in sections:
        s: dict = {"title": sec.title}
        if sec.loinc:
            s["code"] = {"coding": [{"system": "http://loinc.org", "code": sec.loinc,
                                     **({"display": sec.loinc_display} if sec.loinc_display else {})}]}
        s["text"] = {"status": "additional", "div": section_div(sec)}
        comp["section"].append(s)

    inhalt = hashlib.sha256("\x1e".join(s["text"]["div"] for s in comp["section"]).encode("utf-8")).hexdigest()
    entries = [(comp_url, comp), (pat_url, patient)] + ([(f"urn:uuid:{encounter['id']}", encounter)] if encounter else [])
    if cfg.get("ziel") == "transaktion":
        def ident(system: str, value: str) -> str:
            return "identifier=" + quote(f"{system}|{value}", safe="")
        req = {"Composition": {"method": "PUT", "url": "Composition?" + ident(sys["dokument"], m.doc_id)},
               "Patient": {"method": "POST", "url": "Patient", "ifNoneExist": ident(sys["patient"], m.patient_id)},
               "Encounter": {"method": "POST", "url": "Encounter", "ifNoneExist": ident(sys["fall"], m.encounter_id)}}
        order = sorted(entries, key=lambda e: e[1]["resourceType"] == "Composition")
        bundle = {"resourceType": "Bundle", "type": "transaction",
                  "entry": [{"fullUrl": u, "resource": r, "request": req[r["resourceType"]]} for u, r in order]}
    else:
        bundle = {"resourceType": "Bundle", "meta": {"profile": [ISIK_BUNDLE]},
                  "identifier": {"system": "urn:ietf:rfc:3986",
                                 "value": "urn:uuid:" + _uid("bundle", sys["dokument"], m.doc_id, inhalt)},
                  "type": "document", "timestamp": now.isoformat(),
                  "entry": [{"fullUrl": u, "resource": r} for u, r in entries]}
    return bundle, f + check_bundle(bundle)


# --------------------------------------------------------------------------
# Eingebaute Prüfung
# --------------------------------------------------------------------------

_XHTML_OK = {"div", "p", "b", "i", "br", "h1", "h2", "h3", "h4", "table", "tr", "td", "th", "thead", "tbody",
             "ul", "ol", "li", "span", "pre", "em", "strong", "sub", "sup", "small", "a", "hr", "caption"}
_DT = re.compile(r"^\d{4}(-\d{2}(-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2}))?)?)?$")


def _xhtml_problem(div: str) -> str:
    try:
        root = ET.fromstring(div)
    except ET.ParseError as exc:
        return f"kein wohlgeformtes XHTML ({exc})"
    if root.tag != f"{{{XHTML}}}div":
        return "Wurzel ist kein div im XHTML-Namensraum"
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag not in _XHTML_OK:
            return f"Element <{tag}> ist im Narrativ nicht erlaubt"
    if not "".join(root.itertext()).strip():
        return "Narrativ ist leer"
    return ""


def check_bundle(bundle: dict) -> list[Finding]:
    """Prüft die Regeln aus ISiKBerichtBundle und ISiKBerichtSubSysteme, die sich ohne Validator prüfen lassen."""
    probs: list[tuple[str, str]] = []
    entries = bundle.get("entry") or []
    res = [e.get("resource", {}) for e in entries]
    urls = [e.get("fullUrl") for e in entries]
    comps = [r for r in res if r.get("resourceType") == "Composition"]
    if len(urls) != len(set(urls)) or not all(urls):
        probs.append(("Bundle.entry.fullUrl", "fullUrl fehlt oder ist nicht eindeutig"))
    if len(comps) != 1 or sum(r.get("resourceType") == "Patient" for r in res) != 1:
        probs.append(("Bundle.entry", "genau eine Composition und genau ein Patient erwartet"))
    if bundle.get("type") == "document":
        ident = bundle.get("identifier") or {}
        if not (ident.get("system") and ident.get("value")):
            probs.append(("Bundle.identifier", "system und value sind Pflicht"))
        if not bundle.get("timestamp"):
            probs.append(("Bundle.timestamp", "fehlt"))
        if not res or res[0].get("resourceType") != "Composition":
            probs.append(("Bundle.entry[0]", "der erste Eintrag muss die Composition sein"))

    def refs(node):
        if isinstance(node, dict):
            if isinstance(node.get("reference"), str):
                yield node["reference"]
            for v in node.values():
                yield from refs(v)
        elif isinstance(node, list):
            for v in node:
                yield from refs(v)

    for r in res:
        for ref in refs(r):
            if ref not in urls:
                probs.append((r.get("resourceType", "?"), f"Verweis {ref} zeigt auf nichts im Bundle"))
    for c in comps:
        if (c.get("text") or {}).get("status") != "extensions":
            probs.append(("Composition.text.status", "muss „extensions“ sein"))
        p = _xhtml_problem((c.get("text") or {}).get("div", ""))
        if p:
            probs.append(("Composition.text.div", p))
        if not (c.get("identifier") or {}).get("value"):
            probs.append(("Composition.identifier", "fehlt"))
        if c.get("status") != "final":
            probs.append(("Composition.status", "muss „final“ sein"))
        for cd in (c.get("type") or {}).get("coding", []):
            if cd.get("system") == KDL and not re.match(r"^[A-Z]{2}[0-9]{6}$", cd.get("code", "")):
                probs.append(("Composition.type", f"KDL-Code „{cd.get('code')}“ hat nicht die Form AA000000"))
        if not (c.get("subject") or {}).get("reference"):
            probs.append(("Composition.subject", "fehlt"))
        if not _DT.match(c.get("date") or ""):
            probs.append(("Composition.date", f"„{c.get('date')}“ ist kein gültiges FHIR-Datum (Uhrzeit braucht Zeitzone)"))
        if not (c.get("author") or [{}])[0].get("display"):
            probs.append(("Composition.author", "author.display fehlt"))
        if not c.get("title"):
            probs.append(("Composition.title", "fehlt"))
        if not c.get("section"):
            probs.append(("Composition.section", "mindestens ein Abschnitt nötig"))
        for i, s in enumerate(c.get("section") or []):
            if not s.get("title"):
                probs.append((f"Composition.section[{i}].title", "fehlt"))
            p = _xhtml_problem((s.get("text") or {}).get("div", ""))
            if p:
                probs.append((f"Composition.section[{i}].text ({s.get('title')})", p))
    return [Finding("FHIR_REGEL", FEHLER, w, f"Das erzeugte Bundle verletzt eine Profilregel: {msg}.",
                    "Das ist ein Fehler in Stellwerk oder in der Vorlage. Bundle wird nicht zugestellt.") for w, msg in probs]
