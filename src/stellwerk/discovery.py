"""Datenbank erschließen: Schema lesen, Bedeutung der Felder vorschlagen, Zusammenhänge finden.

Alles hier ist Vorschlag. Was Stellwerk nicht sicher erkennt, bleibt "ungeklärt" und geht an den Menschen;
dessen Entscheidungen werden gespeichert und haben immer Vorrang. Gelesen wird ausschließlich (SELECT).
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .fieldmap import LOINC_VORSCHLAG, _ZAHL

# Bedeutungen, die Stellwerk kennt: Schlüssel -> (Bezeichnung, Namensmuster)
BEDEUTUNGEN: dict[str, tuple[str, str]] = {
    "patient_id": ("Patientennummer", r"^(pat(ient(en)?)?_?(id|nr|nummer)|pid|patnr)$"),
    "fall_id": ("Fallnummer", r"^(fall_?(id|nr|nummer)|aufnahme_?(nr|nummer)|case_?id)$"),
    "dokument_id": ("Dokumentnummer", r"^(dok(ument)?_?(id|nr|nummer)|brief_?(id|nr)|doc_?id)$"),
    "auftrag_id": ("Auftrags- oder Befundnummer", r"^((auftrag|befund|order|anforderung)s?_?(id|nr|nummer))$"),
    "nachname": ("Nachname", r"^(nachname|familienname|name|pat_?name)$"),
    "vorname": ("Vorname", r"^(vorname|pat_?vorname|rufname)$"),
    "geburtsdatum": ("Geburtsdatum", r"^(geb(urts)?_?(dat(um)?|tag)|gebdat|birthdate|geboren)$"),
    "geschlecht": ("Geschlecht", r"^(geschlecht|sex|gender)$"),
    "fallart": ("Fallart", r"^(fall_?art|falltyp|behandlungsart|patientenklasse)$"),
    "zeitpunkt": ("Zeitpunkt", r"(zeit|datum|_dat|_ts|_am|timestamp|created|erstellt)$"),
    "autor": ("Verfasser", r"^(arzt|autor|verfasser|ersteller|unterzeichner|freigeber)$"),
    "titel": ("Titel", r"^(titel|betreff|bezeichnung|title)$"),
    "einheit": ("Einheit zu einem Messwert", r"(_einheit|_unit|_eh)$|^(einheit|unit)$"),
    "status": ("Status", r"(status|zustand|freigabe)$"),
    "dokumenttext": ("Dokumenttext", r"(text|_txt|brief|inhalt|befundtext|epikrise|anamnese|diagnosen?|medikation|empfehlung|verlauf)$"),
    "messwert": ("Messwert", r"(_wert|_value|_val|ergebnis)$"),
    "kommentar": ("Kommentar oder interner Vermerk", r"(kommentar|bemerkung|vermerk|notiz|hinweis)$"),
    "schluessel": ("Technischer Schlüssel", r"^(id|lfd_?nr|rowid|pk)$|_id$"),
}
UNGEKLAERT = "ungeklaert"


class DiscoveryError(Exception):
    pass


def crawl(db: str, sample: int = 200, with_values: bool = True) -> dict:
    """Schema lesen. sample > 0 liest je Tabelle so viele Zeilen für das Profil (nur lesend)."""
    if db.startswith("sqlite:///"):
        path = Path(db[10:])
        if not path.exists():
            raise DiscoveryError(f"Datenbankdatei nicht gefunden: {path}")
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            tables = []
            for t in names:
                q = '"' + t.replace('"', '""') + '"'
                cols = [{"name": r[1], "typ": (r[2] or "").upper(), "pflicht": bool(r[3]), "pk": bool(r[5])} for r in con.execute(f"PRAGMA table_info({q})")]
                fks = [{"spalte": r[3], "tabelle": r[2], "ziel": r[4]} for r in con.execute(f"PRAGMA foreign_key_list({q})")]
                n = con.execute(f"SELECT COUNT(*) FROM {q}").fetchone()[0]
                rows = con.execute(f"SELECT * FROM {q} LIMIT {int(sample)}").fetchall() if sample and with_values else []
                tables.append({"name": t, "zeilen": n, "spalten": cols, "fremdschluessel": fks, "_rows": rows})
        except sqlite3.Error as exc:
            raise DiscoveryError(f"Die Datenbank meldet: {exc}") from exc
        finally:
            con.close()
        return {"tabellen": tables}
    try:
        import sqlalchemy
    except ImportError as exc:
        raise DiscoveryError("Für andere Datenbanken als SQLite wird SQLAlchemy samt Treiber gebraucht (pip install 'stellwerk[sql]').") from exc
    try:
        eng = sqlalchemy.create_engine(db)
        insp = sqlalchemy.inspect(eng)
        tables = []
        with eng.connect() as con:
            for t in sorted(insp.get_table_names()):
                pk = set(insp.get_pk_constraint(t).get("constrained_columns") or [])
                cols = [{"name": c["name"], "typ": str(c["type"]).upper(), "pflicht": not c.get("nullable", True), "pk": c["name"] in pk} for c in insp.get_columns(t)]
                fks = [{"spalte": a, "tabelle": fk["referred_table"], "ziel": b} for fk in insp.get_foreign_keys(t)
                       for a, b in zip(fk["constrained_columns"], fk["referred_columns"])]
                tab = sqlalchemy.table(t, *[sqlalchemy.column(c["name"]) for c in cols])
                n = con.execute(sqlalchemy.select(sqlalchemy.func.count()).select_from(tab)).scalar()
                rows = [tuple(r) for r in con.execute(sqlalchemy.select(tab).limit(int(sample))).fetchall()] if sample and with_values else []
                tables.append({"name": t, "zeilen": n, "spalten": cols, "fremdschluessel": fks, "_rows": rows})
    except sqlalchemy.exc.SQLAlchemyError as exc:
        raise DiscoveryError(f"Die Datenbank meldet: {str(exc).splitlines()[0]}") from exc
    return {"tabellen": tables}


def _meaning(col: str, typ: str, vals: list[str]) -> tuple[str, str, str, dict]:
    """(Bedeutung, Sicherheit, Begründung, Zusatz) für eine Spalte."""
    low = col.strip().lower()
    hit = next((x for x in LOINC_VORSCHLAG if any(low == t or low.startswith(t) for t in x[0])), None)
    if hit and not re.search(r"(_einheit|_unit|_eh)$", low):
        zahlen = sum(1 for v in vals if _ZAHL.match(v))
        if not vals or zahlen >= 0.6 * len(vals):
            return "messwert", "mittel", f"Name passt zu einem Laborwert (LOINC {hit[1]}, {hit[2]})" + (
                f"; {zahlen} von {len(vals)} Beispielwerten sind Zahlen" if vals else ""), {"loinc": hit[1], "anzeige": hit[2]}
    for key, (_, pat) in BEDEUTUNGEN.items():
        if re.search(pat, low):
            sicher = "hoch" if pat.startswith("^") and "|" not in pat.split("$")[-1] and key not in ("schluessel", "nachname") else "mittel"
            grund = f"Spaltenname „{col}“ ist eine übliche Bezeichnung für {BEDEUTUNGEN[key][0]}"
            if key == "dokumenttext" and vals and max(len(v) for v in vals) < 40:
                return "kommentar", "niedrig", f"Name klingt nach Text, die Werte sind aber kurz (höchstens {max(len(v) for v in vals)} Zeichen)", {}
            if key == "nachname" and low == "name":
                sicher, grund = "niedrig", "„name“ kann ein Personenname, aber auch eine Bezeichnung sein"
            return key, sicher, grund, {}
    if vals and all(re.match(r"^\d{1,2}\.\d{1,2}\.\d{4}|^\d{4}-\d{2}-\d{2}", v) for v in vals):
        return "zeitpunkt", "mittel", "Alle Beispielwerte sind Datumsangaben", {}
    if vals and max(len(v) for v in vals) > 120:
        return "dokumenttext", "niedrig", "Lange Freitexte in den Beispielwerten", {}
    return UNGEKLAERT, "", "Weder Name noch Werte lassen eine Bedeutung sicher erkennen", {}


def _klasse(bed: set[str]) -> str:
    if "messwert" in bed:
        return "Messwerte"
    if "dokumenttext" in bed and ("dokument_id" in bed or "autor" in bed):
        return "Dokumente"
    if {"nachname", "geburtsdatum"} <= bed and "fall_id" not in bed:
        return "Patientenstamm"
    if "fall_id" in bed and "patient_id" in bed and "dokumenttext" not in bed:
        return "Fälle"
    return "unklar"


def analyse(schema: dict, entscheidungen: dict | None = None, llm=None) -> dict:
    """Bedeutungen und Zusammenhänge vorschlagen. entscheidungen: {"tabelle.spalte": bedeutung} vom Menschen."""
    ent = entscheidungen or {}
    tables = []
    values: dict[tuple[str, str], set[str]] = {}
    for t in schema["tabellen"]:
        rows = t.get("_rows") or []
        cols = []
        for i, c in enumerate(t["spalten"]):
            vals = [str(r[i]).strip() for r in rows if r[i] not in (None, "")]
            values[(t["name"], c["name"])] = set(vals)
            key = f"{t['name']}.{c['name']}"
            bed, sicher, grund, extra = _meaning(c["name"], c["typ"], vals)
            col = {**c, "bedeutung": bed, "sicherheit": sicher, "begruendung": grund, "quelle": "regel", **extra,
                   "leer": (len(rows) - len(vals)) if rows else None, "verschieden": len(set(vals)) if rows else None}
            if key in ent:
                col.update(bedeutung=ent[key], sicherheit="hoch", begruendung="Von Hand festgelegt", quelle="mensch")
            cols.append(col)
        tables.append({"name": t["name"], "zeilen": t["zeilen"], "spalten": cols, "fremdschluessel": t["fremdschluessel"]})

    # --- Ungeklärtes optional dem Modell vorlegen (nur aus der Liste der bekannten Bedeutungen)
    offen = [(t, c) for t in tables for c in t["spalten"] if c["bedeutung"] == UNGEKLAERT]
    if llm and offen:
        labels = [{"key": k, "titel": v[0], "beschreibung": ""} for k, v in BEDEUTUNGEN.items()] + [
            {"key": UNGEKLAERT, "titel": "nicht erkennbar", "beschreibung": ""}]
        items = []
        for i, (t, c) in enumerate(offen):
            text = f"Tabelle {t['name']}, Spalte {c['name']}, Typ {c['typ'] or 'unbekannt'}"
            if getattr(llm, "patientendaten", False):
                text += "\nBeispielwerte: " + ", ".join(sorted(values[(t["name"], c["name"])])[:3])
            items.append((i, text))
        try:
            got = llm.classify(items, labels, UNGEKLAERT)
        except Exception:
            got = {}
        for i, (key, conf) in got.items():
            if key != UNGEKLAERT:
                offen[i][1].update(bedeutung=key, sicherheit="niedrig", quelle="modell",
                                   begruendung=f"Vorschlag des Modells (eigene Angabe: {conf}). Nicht geprüft.")

    # --- Zusammengehörige Felder innerhalb einer Tabelle
    for t in tables:
        gruppen = []
        names = [c["name"] for c in t["spalten"]]
        stem = lambda n: re.sub(r"(_?(wert|value|val|einheit|unit|eh))$", "", n.lower())     # noqa: E731
        for c in t["spalten"]:
            if c["bedeutung"] == "einheit":
                partner = [n for n in names if n != c["name"] and stem(n) == stem(c["name"])]
                if partner:
                    gruppen.append({"art": "Wert und Einheit", "spalten": [partner[0], c["name"]], "begruendung": "gleicher Namensstamm"})
        person = [c["name"] for c in t["spalten"] if c["bedeutung"] in ("nachname", "vorname", "geburtsdatum", "geschlecht")]
        if len(person) >= 2:
            gruppen.append({"art": "Angaben zur Person", "spalten": person, "begruendung": "gehören zum selben Patienten"})
        texte = [c["name"] for c in t["spalten"] if c["bedeutung"] == "dokumenttext"]
        if len(texte) >= 2:
            gruppen.append({"art": "Abschnitte eines Dokuments", "spalten": texte, "begruendung": "mehrere Textspalten in einer Zeile"})
        t["gruppen"] = gruppen
        t["klasse"] = _klasse({c["bedeutung"] for c in t["spalten"]})

    # --- Beziehungen zwischen Tabellen: erklärte Fremdschlüssel, sonst gleiche Bedeutung plus Werteüberdeckung
    bez = []
    for t in tables:
        for fk in t["fremdschluessel"]:
            bez.append({"von": f"{t['name']}.{fk['spalte']}", "nach": f"{fk['tabelle']}.{fk['ziel']}", "art": "Fremdschlüssel",
                        "sicherheit": "hoch", "begruendung": "In der Datenbank als Fremdschlüssel erklärt"})
    erklaert = {(b["von"], b["nach"]) for b in bez} | {(b["nach"], b["von"]) for b in bez}
    keys = ("patient_id", "fall_id", "dokument_id", "auftrag_id")
    for i, a in enumerate(tables):
        for b in tables[i + 1:]:
            for ca in a["spalten"]:
                for cb in b["spalten"]:
                    if ca["bedeutung"] not in keys or ca["bedeutung"] != cb["bedeutung"]:
                        continue
                    von, nach = f"{a['name']}.{ca['name']}", f"{b['name']}.{cb['name']}"
                    if (von, nach) in erklaert:
                        continue
                    va, vb = values[(a["name"], ca["name"])], values[(b["name"], cb["name"])]
                    if va and vb:
                        klein = va if len(va) <= len(vb) else vb
                        anteil = len(va & vb) / len(klein)
                        if anteil == 0:
                            continue
                        sicher = "mittel" if anteil >= 0.8 else "niedrig"
                        grund = f"Beide Spalten bedeuten {BEDEUTUNGEN[ca['bedeutung']][0]}; {round(100 * anteil)} % der Beispielwerte kommen in beiden vor"
                    else:
                        sicher, grund = "niedrig", f"Beide Spalten bedeuten {BEDEUTUNGEN[ca['bedeutung']][0]}; Werte wurden nicht verglichen"
                    bez.append({"von": von, "nach": nach, "art": "vermutet", "sicherheit": sicher, "begruendung": grund})
    n_cols = sum(len(t["spalten"]) for t in tables)
    n_offen = sum(1 for t in tables for c in t["spalten"] if c["bedeutung"] == UNGEKLAERT)
    n_pruefen = sum(1 for t in tables for c in t["spalten"] if c["bedeutung"] != UNGEKLAERT and c["sicherheit"] == "niedrig")
    return {"tabellen": tables, "beziehungen": bez,
            "bedeutungen": [{"key": k, "titel": v[0]} for k, v in BEDEUTUNGEN.items()],
            "zusammenfassung": {"tabellen": len(tables), "spalten": n_cols, "ungeklaert": n_offen, "unsicher": n_pruefen,
                                "beziehungen": len(bez), "vermutet": sum(1 for b in bez if b["art"] == "vermutet")}}


def render_text(res: dict) -> str:
    z = res["zusammenfassung"]
    L = [f"{z['tabellen']} Tabellen, {z['spalten']} Spalten gelesen. {z['ungeklaert']} Spalten ungeklärt, {z['unsicher']} unsicher. "
         f"{z['beziehungen']} Beziehungen, davon {z['vermutet']} vermutet.", ""]
    for t in res["tabellen"]:
        L.append(f"{t['name']}  ({t['zeilen']} Zeilen, Art: {t['klasse']})")
        for c in t["spalten"]:
            titel = BEDEUTUNGEN.get(c["bedeutung"], ("ungeklärt",))[0]
            mark = "?" if c["bedeutung"] == UNGEKLAERT else "!" if c["sicherheit"] == "niedrig" else "✓"
            L.append(f"  {mark} {c['name']:<22} {titel:<30} {c.get('loinc', ''):<8} {c['sicherheit'] or '–'}")
        for g in t["gruppen"]:
            L.append(f"    gehört zusammen ({g['art']}): {', '.join(g['spalten'])}")
        L.append("")
    if res["beziehungen"]:
        L.append("Beziehungen")
        L += [f"  {b['von']} → {b['nach']}  ({b['art']}, {b['sicherheit']}): {b['begruendung']}" for b in res["beziehungen"]]
    return "\n".join(L).rstrip() + "\n"
