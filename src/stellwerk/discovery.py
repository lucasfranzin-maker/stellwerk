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


_ZAHLTYP = re.compile(r"INT|SERIAL|NUMERIC|DECIMAL|NUMBER|FLOAT|DOUBLE|REAL", re.I)
_ZEITTYP = re.compile(r"TIMESTAMP|DATETIME", re.I)

def crawl(db: str, sample: int = 200, with_values: bool = True) -> dict:
    """Schema lesen. sample > 0 liest je Tabelle so viele Zeilen für das Profil (nur lesend)."""
    from .sqlsource import SqlError, resolve_url
    try:
        db = resolve_url(db)
    except SqlError as exc:
        raise DiscoveryError(str(exc)) from exc
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
    except ImportError as exc:
        from .sqlsource import driver_missing
        raise DiscoveryError(driver_missing(exc)) from exc
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
        tables.append({"name": t["name"], "zeilen": t["zeilen"], "stichprobe": len(rows), "spalten": cols,
                       "fremdschluessel": t["fremdschluessel"]})

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


# --------------------------------------------------------------------------
# Abfragen vorschlagen
# --------------------------------------------------------------------------

_STAMM = ("Patientenstamm", "Fälle")
_WUNSCH = ("patient_id", "nachname", "vorname", "geburtsdatum", "geschlecht", "fall_id", "fallart")
_RESERVIERT = {"user", "order", "group", "table", "select", "from", "where", "date", "comment", "index", "key", "level", "size",
               "number", "type", "value", "values", "start", "end", "desc", "asc", "by", "to", "in", "is", "as", "on", "or", "and", "not"}
_RANG = {"hoch": 0, "mittel": 1, "niedrig": 2, "": 3}


def _q(name: str) -> str:
    """Bezeichner nur dann in Anführungszeichen setzen, wenn es nötig ist (Oracle unterscheidet dann Groß- und Kleinschreibung)."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$#]*", name) and name.lower() not in _RESERVIERT:
        return name
    return '"' + name.replace('"', '""') + '"'


def propose_queries(res: dict) -> list[dict]:
    """Je Tabelle mit Messwerten oder Dokumenten ein fertiges SELECT, bei Bedarf mit Verknüpfung zu Fall und Patient.

    Verknüpft wird nur in Richtung Stammdaten und mit LEFT JOIN: keine Zeile der Ausgangstabelle geht verloren.
    Vermutete Verknüpfungen stehen als solche im SQL und machen den Vorschlag „zu prüfen“."""
    tabs = {t["name"]: t for t in res["tabellen"]}
    kanten: dict[str, list[tuple[str, str, str, dict]]] = {}
    for b in res["beziehungen"]:
        (ta, ca), (tb, cb) = b["von"].rsplit(".", 1), b["nach"].rsplit(".", 1)
        kanten.setdefault(ta, []).append((ca, tb, cb, b))
        kanten.setdefault(tb, []).append((cb, ta, ca, b))
    out = []
    for t in res["tabellen"]:
        if t["klasse"] not in ("Messwerte", "Dokumente"):
            continue
        hat = {c["bedeutung"] for c in t["spalten"]}
        fehlt = [w for w in _WUNSCH if w not in hat]
        # Breitensuche über Stammtabellen, höchstens zwei Schritte weit
        eltern: dict[str, tuple[str, str, str, dict] | None] = {t["name"]: None}      # Tabelle -> (von, von_spalte, eigene_spalte, Beziehung)
        reihenfolge, rand = [], [t["name"]]
        for _ in range(2):
            neu = []
            for name in rand:
                for c_own, other, c_other, b in sorted(kanten.get(name, []), key=lambda k: (k[3]["art"] != "Fremdschlüssel", _RANG[k[3]["sicherheit"]])):
                    if other not in eltern and tabs[other]["klasse"] in _STAMM:
                        eltern[other] = (name, c_own, c_other, b)
                        reihenfolge.append(other)
                        neu.append(other)
            rand = neu
        liefert: dict[str, list[dict]] = {}
        offen = list(fehlt)
        for name in reihenfolge:
            cols = []
            for c in tabs[name]["spalten"]:
                if c["bedeutung"] in offen:
                    offen.remove(c["bedeutung"])
                    cols.append(c)
            if cols:
                liefert[name] = cols
        gebraucht: list[str] = []
        for name in liefert:                                   # auch die Tabellen auf dem Weg dorthin
            kette = []
            while name != t["name"]:
                kette.append(name)
                name = eltern[name][0]
            for k in reversed(kette):
                if k not in gebraucht:
                    gebraucht.append(k)
        alias: dict[str, str] = {}
        for name in [t["name"], *gebraucht]:
            kurz = re.sub(r"[^a-z]", "", name.rsplit(".", 1)[-1].lower())[:1] or "t"
            a, n = kurz, 2
            while a in alias.values() or a in _RESERVIERT:
                a, n = f"{kurz}{n}", n + 1
            alias[name] = a
        namen: set[str] = set()
        zeilen_sql = []
        for name in [t["name"], *gebraucht]:
            for c in (t["spalten"] if name == t["name"] else liefert.get(name, [])):
                ausdruck = f"{alias[name]}.{_q(c['name'])}"
                if c["name"].lower() in namen:
                    ausdruck += " AS " + _q(f"{name.rsplit('.', 1)[-1]}_{c['name']}")
                namen.add(c["name"].lower())
                zeilen_sql.append(ausdruck)
        verkn, hinweise, teile = [], [], []
        von_sql = [f"FROM {_q(t['name'])} {alias[t['name']]}"]
        sql = [f"-- Vorschlag von Stellwerk: {t['klasse']} aus {t['name']}" + (f" mit {', '.join(gebraucht)}" if gebraucht else ""),
               "-- Nur lesend." + (" Verknüpfungen stehen als LEFT JOIN, damit keine Zeile verloren geht." if gebraucht else ""),
               "SELECT", ",\n".join("  " + z for z in zeilen_sql), f"FROM {_q(t['name'])} {alias[t['name']]}"]
        for name in gebraucht:
            von, c_von, c_own, b = eltern[name]
            vermutet = b["art"] != "Fremdschlüssel"
            on = f"{alias[name]}.{_q(c_own)} = {alias[von]}.{_q(c_von)}"
            sql += ["-- " + (f"VERMUTET ({b['sicherheit']}), bitte prüfen:" if vermutet else "erklärter Fremdschlüssel:"),
                    f"LEFT JOIN {_q(name)} {alias[name]} ON {on}"]
            von_sql.append(f"LEFT JOIN {_q(name)} {alias[name]} ON {on}")
            teile.append((name, f"{alias[name]}.{_q(c_own)} IS NULL AND {alias[von]}.{_q(c_von)} IS NOT NULL"))
            verkn.append({"tabelle": name, "ueber": on, "art": b["art"], "sicherheit": b["sicherheit"], "begruendung": b["begruendung"]})
            if vermutet:
                hinweise.append(f"Die Verknüpfung mit {name} ({on}) ist vermutet: {b['begruendung']}. Am Datenmodell oder mit dem Hersteller prüfen.")
            ziel = next(c for c in tabs[name]["spalten"] if c["name"] == c_own)
            n = tabs[name]["stichprobe"] - (ziel["leer"] or 0) if tabs[name]["stichprobe"] else 0
            if not ziel["pk"] and n and ziel["verschieden"] is not None and ziel["verschieden"] < n:
                hinweise.append(f"{name}.{c_own} ist in der Stichprobe nicht eindeutig ({ziel['verschieden']} verschiedene Werte in {n} Zeilen). "
                                "Die Verknüpfung kann Zeilen vervielfachen.")
        if "patient_id" in offen:
            hinweise.append("Die Abfrage liefert keine Patientennummer. Ohne sie entsteht kein Bundle; die Tabelle mit der Patientennummer von Hand verknüpfen.")
        unklar = [c["name"] for c in t["spalten"] if c["bedeutung"] == UNGEKLAERT]
        if unklar:
            hinweise.append(f"Bedeutung ungeklärt: {', '.join(unklar)}. Die Spalten stehen in der Abfrage; in der Zuordnung entscheiden, ob sie gebraucht werden.")
        # Der BridgeLink-Kanal braucht eine Spalte, die je Zeile steigt und sich vergleichen lässt: Zahl oder echter Zeitstempel, nie Text.
        zahl = [c["name"] for c in t["spalten"] if _ZAHLTYP.search(c["typ"]) and (c["pk"] or c["bedeutung"] in ("auftrag_id", "dokument_id"))]
        zeit = [c["name"] for c in t["spalten"] if _ZEITTYP.search(c["typ"]) and c["bedeutung"] == "zeitpunkt"]
        kand = [f"{n} (laufende Nummer)" for n in zahl] + [f"{n} (Zeitstempel)" for n in zeit]
        hinweise.append("Für den laufenden Betrieb braucht die Abfrage eine Spalte, die je Zeile steigt, als Zahl oder Zeitstempel gespeichert "
                        + (f"(Kandidaten: {', '.join(kand)})." if kand else "(keine passende Spalte erkannt; im KIS eine laufende Nummer erfragen oder in einer Sicht ergänzen).")
                        + " Text, auch Datumswerte als Text, lässt sich nicht zuverlässig vergleichen.")
        out.append({"name": f"{t['klasse']} aus {t['name']}", "tabelle": t["name"], "klasse": t["klasse"],
                    "modus": "felder" if t["klasse"] == "Messwerte" else "dokument", "sql": "\n".join(sql) + "\n",
                    "verknuepfungen": verkn, "hinweise": hinweise,
                    "pruefen": any(v["art"] != "Fremdschlüssel" for v in verkn) or "patient_id" in offen, "zeilen": None,
                    "_teile": ("\n".join(von_sql), teile)})
    return out


def verify_queries(db: str, res: dict, abfragen: list[dict]) -> None:
    """Jeden Vorschlag einmal zählen lassen: läuft er, und liefert er so viele Zeilen wie die Ausgangstabelle?"""
    from .sqlsource import SqlError, run_query
    soll = {t["name"]: t["zeilen"] for t in res["tabellen"]}
    for a in abfragen:
        von_sql, teile = a.pop("_teile", ("", []))
        try:
            _, rows = run_query(db, f"SELECT COUNT(*) AS n FROM (\n{a['sql']}) q")
            a["zeilen"] = int(rows[0][0])
            for name, bedingung in teile:                      # Zeilen, die in der Stammtabelle keinen Partner finden
                _, rows = run_query(db, f"SELECT COUNT(*) AS n {von_sql} WHERE {bedingung}")
                if int(rows[0][0]):
                    a["pruefen"] = True
                    a["hinweise"].insert(0, f"{int(rows[0][0])} von {a['zeilen']} Zeilen finden keinen Eintrag in {name}. "
                                            "Sie bleiben in der Ausgabe, aber ohne die Angaben von dort.")
        except (SqlError, ValueError, IndexError, TypeError) as exc:
            a["pruefen"] = True
            a["hinweise"].insert(0, f"Die Datenbank hat die Abfrage nicht angenommen: {exc}")
            continue
        if a["zeilen"] != soll[a["tabelle"]]:
            a["pruefen"] = True
            a["hinweise"].insert(0, f"Die Abfrage liefert {a['zeilen']} Zeilen, {a['tabelle']} hat {soll[a['tabelle']]}. "
                                    "Eine Verknüpfung vervielfacht Zeilen; die Beziehung prüfen.")


def render_queries(abfragen: list[dict]) -> str:
    if not abfragen:
        return "Keine Tabelle mit Messwerten oder Dokumenten erkannt; kein Vorschlag für eine Abfrage.\n"
    L = []
    for a in abfragen:
        L.append(f"-- {'ZU PRÜFEN' if a['pruefen'] else 'ohne offene Punkte'}: {a['name']}"
                 + (f" ({a['zeilen']} Zeilen)" if a["zeilen"] is not None else ""))
        L += [f"--   {h}" for h in a["hinweise"]]
        L += [a["sql"].rstrip() + ";", ""]
    return "\n".join(L)
