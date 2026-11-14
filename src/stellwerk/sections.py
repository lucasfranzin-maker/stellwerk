"""Text den Abschnitten einer Vorlage zuordnen.

Reihenfolge der Mittel, vom verlässlichsten zum unsichersten:
1. Gliederung, die die Quelle mitliefert (OBX-3, Spaltennamen).
2. Überschriften im Text, die die Vorlage als Alias kennt (auch leicht verlesen).
3. Vorschlag des lokalen Sprachmodells für den Rest. Gilt immer als "zu prüfen".
4. Sammelabschnitt. Es geht keine Zeile verloren.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from importlib import resources
from pathlib import Path

import yaml

from .model import FEHLER, INFO, PRUEFEN, Finding, Line, Part, Section, SourceDoc, zahl

_NUM = re.compile(r"^[\s\-–•*#]*(?:\d{1,2}[.)]|[ivxIVX]{1,4}[.)]|[a-zA-Z][.)])\s+")


def norm(s: str) -> str:
    s = _NUM.sub("", s.strip()).lower().strip(" :.-–*#_=\t")
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9/ ]+", " ", s)).strip()


def ocr_key(s: str) -> str:
    """Typische Verleser der Texterkennung angleichen (nur für den Vergleich)."""
    s = norm(s).translate(str.maketrans("015|", "olsl"))
    return s.replace("rn", "m").replace("vv", "w")


@dataclass
class Template:
    name: str
    titel: str
    kdl: str = ""
    kdl_anzeige: str = ""
    abschnitte: list[dict] = field(default_factory=list)
    pfad: str = ""
    eigen: bool = False              # eigene Datei statt mitgelieferter Vorlage

    def __post_init__(self):
        self.by_key = {a["key"]: a for a in self.abschnitte}
        self.alias: dict[str, str] = {}
        for a in self.abschnitte:
            if a.get("rolle"):
                continue
            for al in [a["titel"], *a.get("aliase", [])]:
                self.alias.setdefault(norm(al), a["key"])
        self._ocr = {ocr_key(k): v for k, v in self.alias.items()}
        self.kopf = next((a["key"] for a in self.abschnitte if a.get("rolle") == "kopf"), None)
        self.rest = next((a["key"] for a in self.abschnitte if a.get("rolle") == "rest"), None)
        self.schluss = next((a for a in self.abschnitte if a.get("rolle") == "schluss"), None)
        if not self.rest:
            self.abschnitte.append({"key": "weitere", "titel": "Weitere Angaben", "rolle": "rest"})
            self.by_key["weitere"] = self.abschnitte[-1]
            self.rest = "weitere"
        self.kopf = self.kopf or self.rest

    def lookup(self, heading: str) -> tuple[str, float] | None:
        """Überschrift -> (Abschnitt, Güte). Güte < 1 heißt: nur ähnlich."""
        k = norm(heading)
        if k in self.alias:
            return self.alias[k], 1.0
        ok = ocr_key(heading)
        if len(ok) < 5:
            return None
        best, best_r = None, 0.0
        for cand, key in self._ocr.items():
            if abs(len(cand) - len(ok)) > 3:
                continue
            r = SequenceMatcher(None, ok, cand).ratio()
            if r > best_r:
                best, best_r = key, r
        if best and best_r >= 0.86:
            return best, min(best_r, 0.95)
        return None


def load_template(name: str) -> Template:
    """Vorlage laden: Dateipfad oder Name einer mitgelieferten Vorlage."""
    p = Path(name)
    if p.suffix in (".yaml", ".yml") and p.exists():
        raw, pfad, eigen = yaml.safe_load(p.read_text(encoding="utf-8")), str(p), True
    else:
        res = resources.files("stellwerk") / "vorlagen" / f"{name}.yaml"
        if not res.is_file():
            raise FileNotFoundError(f"Vorlage „{name}“ nicht gefunden. Mitgeliefert: {', '.join(list_templates())}.")
        raw, pfad, eigen = yaml.safe_load(res.read_text(encoding="utf-8")), name, False
    typ = raw.get("typ") or {}
    return Template(raw["name"], raw.get("titel", raw["name"]), typ.get("kdl") or "", typ.get("anzeige") or "",
                    raw["abschnitte"], pfad, eigen)


def list_templates() -> list[str]:
    return sorted(p.name[:-5] for p in (resources.files("stellwerk") / "vorlagen").iterdir() if p.name.endswith(".yaml"))


@dataclass
class _Chunk:
    heading: str
    key: str | None
    method: str
    score: float
    lines: list[Line] = field(default_factory=list)
    note: str = ""

    def text(self) -> str:
        return "\n".join(ln.text for ln in self.lines if ln.text.strip())

    def filled(self) -> int:
        return sum(1 for ln in self.lines if ln.text.strip())


def _looks_like_heading(t: str) -> bool:
    t = t.strip()
    if not 3 <= len(t) <= 50 or len(t.split()) > 5 or not (t[0].isupper() or t[0].isdigit()):
        return False
    if t.endswith(":") and not re.search(r"[.,;!?]", t[:-1]):
        return True
    letters = [c for c in t if c.isalpha()]
    if len(letters) >= 4 and all(c.isupper() for c in letters) and not re.search(r"\d", t):
        return True
    return bool(re.match(r"^\d{1,2}\.\s+[A-ZÄÖÜ][^.,;!?]*$", t)) and len(t.split()) <= 4


def _match_line(tpl: Template, text: str):
    """Ist die Zeile eine bekannte Überschrift? -> (key, Güte, Überschrift, Resttext)"""
    t = text.strip()
    if not t or len(t) > 200:
        return None
    if len(t) <= 60 and norm(t) in tpl.alias:
        return tpl.alias[norm(t)], 1.0, t.rstrip(": "), ""
    if ":" in t:
        left, right = t.split(":", 1)
        if len(left) <= 45 and norm(left) in tpl.alias:
            return tpl.alias[norm(left)], 1.0, left.strip(), right.strip()
    if len(t) <= 45 and len(t.split()) <= 5 and not t.endswith((".", ",", ";")):
        hit = tpl.lookup(t)
        if hit and hit[1] < 1.0:
            return hit[0], hit[1], t.rstrip(": "), ""
    return None


def sectionize(doc: SourceDoc, tpl: Template, llm=None) -> tuple[list[Section], list[Finding]]:
    findings: list[Finding] = []
    lines = doc.lines
    n_in = sum(1 for ln in lines if ln.text.strip())
    if not n_in:
        return [], findings
    chunks: list[_Chunk] = []
    heading_only = 0
    structured = False

    hints = list(dict.fromkeys(ln.hint for ln in lines if ln.hint))
    if len(hints) >= 2 and any(tpl.lookup(h) for h in hints):
        # --- Die Quelle liefert die Gliederung mit
        structured = True
        cur = None
        for ln in lines:
            if ln.hint and (cur is None or ln.hint != cur.heading):
                hit = tpl.lookup(ln.hint)
                cur = _Chunk(ln.hint, hit[0] if hit else None, "quelle", hit[1] if hit else 0.0)
                chunks.append(cur)
            if cur is None:
                cur = _Chunk("", tpl.kopf, "position", 1.0)
                chunks.append(cur)
            cur.lines.append(ln)
    else:
        # --- Überschriften im Text suchen
        cur = _Chunk("", tpl.kopf, "position", 1.0)
        chunks.append(cur)
        closing = [re.compile(p) for p in (tpl.schluss or {}).get("beginnt_mit", [])]
        for idx, ln in enumerate(lines):
            t = ln.text.strip()
            if t and structured and any(p.search(t) for p in closing):
                cur = _Chunk("", tpl.schluss["key"], "position", 1.0, list(lines[idx:]))
                chunks.append(cur)
                break
            m = _match_line(tpl, ln.text)
            if m:
                key, score, heading, rest = m
                cur = _Chunk(heading, key, "ueberschrift" if score == 1.0 else "unscharf", score)
                chunks.append(cur)
                structured = True
                if rest:
                    cur.lines.append(replace(ln, text=rest))
                else:
                    heading_only += 1
                if score < 1.0:
                    findings.append(Finding(
                        "ABS_UNSCHARF", INFO, ln.origin or "Text",
                        f"Überschrift „{heading}“ als „{tpl.by_key[key]['titel']}“ gelesen (ähnliche Schreibweise).",
                        f"Falls das nicht stimmt: die Schreibweise in der Vorlage {tpl.pfad} dem richtigen Abschnitt als Alias zuordnen."))
            elif structured and _looks_like_heading(ln.text):
                cur = _Chunk(ln.text.strip().rstrip(":").strip(), None, "rest", 0.0)
                chunks.append(cur)
                heading_only += 1
            else:
                cur.lines.append(ln)
        if not structured and closing:                           # ohne Gliederung trotzdem die Grußformel abtrennen
            body = chunks[0].lines
            for idx, ln in enumerate(body):
                if idx and any(p.search(ln.text.strip()) for p in closing):
                    chunks.append(_Chunk("", tpl.schluss["key"], "position", 1.0, body[idx:]))
                    chunks[0].lines = body[:idx]
                    break

    labels = [{"key": a["key"], "titel": a["titel"], "beschreibung": a.get("beschreibung", "")} for a in tpl.abschnitte]
    llm_error = ""

    def ask(items: list[tuple[int, str]]) -> dict[int, tuple[str, str]]:
        nonlocal llm_error
        if not llm or not items:
            return {}
        try:
            return llm.classify(items, labels, tpl.rest)
        except Exception as exc:                                 # LLM darf die Verarbeitung nie abbrechen
            llm_error = str(exc)
            return {}

    if not structured:
        # --- Keine Gliederung: Absätze einzeln zuordnen lassen
        body = chunks[0]
        paras: list[list[Line]] = [[]]
        for ln in body.lines:
            if ln.text.strip():
                paras[-1].append(ln)
            elif paras[-1]:
                paras.append([])
        paras = [p for p in paras if p]
        got = ask([(i, "\n".join(ln.text for ln in p)) for i, p in enumerate(paras)])
        if got:
            new: list[_Chunk] = []
            for i, p in enumerate(paras):
                key, conf = got.get(i, (tpl.rest, "niedrig"))
                method = "ki" if i in got and key != tpl.rest else "rest"
                if new and new[-1].key == key and new[-1].method == method:
                    new[-1].lines += [Line("")] + p
                else:
                    new.append(_Chunk("", key, method, {"hoch": 0.8, "mittel": 0.6}.get(conf, 0.4), list(p), conf))
            chunks = new + chunks[1:]
            n_ki = sum(1 for c in new if c.method == "ki")
            findings.append(Finding(
                "ABS_KI_OHNE_GLIEDERUNG", PRUEFEN, "Gesamter Text",
                f"Der Text hat keine erkennbaren Überschriften. Das lokale Modell hat {len(paras)} Absätze auf "
                f"{n_ki} Abschnittsblöcke verteilt.",
                "Zuordnung im Bericht unter „Zuordnung Zeile für Zeile“ gegenlesen."))
        else:
            body.key, body.method = tpl.rest, "rest"
            findings.append(Finding(
                "ABS_OHNE_GLIEDERUNG", PRUEFEN, "Gesamter Text",
                "Der Text hat keine Überschriften, die die Vorlage kennt. Alles steht unter "
                f"„{tpl.by_key[tpl.rest]['titel']}“.",
                f"Überschriften des Dokuments in der Vorlage {tpl.pfad} als Aliase ergänzen"
                + ("." if llm else " oder ein lokales Modell einstellen (llm.modell).")))
    else:
        unknown = [(i, c) for i, c in enumerate(chunks) if c.key is None]
        got = ask([(i, f"Überschrift: {c.heading}\n{c.text()}") for i, c in unknown])
        for i, c in unknown:
            if i in got and got[i][0] != tpl.rest:
                key, conf = got[i]
                a = tpl.by_key[key]
                sig = sum(1 for p in a.get("signale", []) if re.search(p, c.text()))
                c.key, c.method, c.score, c.note = key, "ki", {"hoch": 0.8, "mittel": 0.6}.get(conf, 0.4), conf
                findings.append(Finding(
                    "ABS_KI", PRUEFEN, f"Überschrift „{c.heading}“",
                    f"Die Vorlage kennt „{c.heading}“ nicht. Das lokale Modell schlägt „{a['titel']}“ vor "
                    f"(Sicherheit {conf}" + (", typische Stichwörter kommen vor" if sig else "") + f"; {zahl(c.filled(), 'Zeile', 'Zeilen')}).",
                    f"Wenn richtig: in der Vorlage {tpl.pfad} beim Abschnitt {key} unter aliase „{c.heading}“ ergänzen. "
                    "Dann wird künftig ohne Modell und ohne Rückfrage zugeordnet.",
                    {"alias": c.heading, "abschnitt": key}))
            else:
                c.key = tpl.rest
                if c.filled():
                    findings.append(Finding(
                        "ABS_UNBEKANNT", PRUEFEN, f"Überschrift „{c.heading}“",
                        f"Die Vorlage kennt „{c.heading}“ nicht. Der Inhalt ({zahl(c.filled(), 'Zeile', 'Zeilen')}) steht unter "
                        f"„{tpl.by_key[tpl.rest]['titel']}“.",
                        f"In der Vorlage {tpl.pfad} beim passenden Abschnitt unter aliase „{c.heading}“ ergänzen.",
                        {"alias": c.heading}))

    if llm_error:
        findings.append(Finding("LLM_FEHLER", PRUEFEN, "Lokales Modell",
                                f"Das lokale Modell konnte nicht helfen: {llm_error} Ohne Modell weitergemacht.",
                                "Mit „stellwerk doctor“ prüfen, ob der Server läuft und das Modell geladen ist."))

    # --- Abschnitte in Dokumentreihenfolge zusammenbauen
    sections: dict[str, Section] = {}
    for c in chunks:
        while c.lines and not c.lines[0].text.strip():
            c.lines.pop(0)
        while c.lines and not c.lines[-1].text.strip():
            c.lines.pop()
        if not c.lines:
            if c.heading and c.method != "rest":
                findings.append(Finding("ABS_LEER", INFO, f"Überschrift „{c.heading}“",
                                        "Überschrift ohne Inhalt; Abschnitt ausgelassen."))
            continue
        a = tpl.by_key[c.key]
        sec = sections.setdefault(c.key, Section(c.key, a["titel"], a.get("loinc", ""), a.get("loinc_anzeige", "")))
        shown = c.heading if c.heading and norm(c.heading) != norm(a["titel"]) else ""
        sec.parts.append(Part(shown, c.lines, c.method, c.score, c.note))

    for a in tpl.abschnitte:
        if a.get("pflicht") and a["key"] not in sections:
            findings.append(Finding(
                "ABS_FEHLT", PRUEFEN, f"Abschnitt „{a['titel']}“",
                f"Der Pflichtabschnitt „{a['titel']}“ wurde nicht gefunden.",
                f"Steht er unter einer anderen Überschrift? Dann diese in der Vorlage {tpl.pfad} als Alias ergänzen."))

    n_out = sum(1 for s in sections.values() for ln in s.lines if ln.text.strip()) + heading_only
    if n_out != n_in:
        findings.append(Finding(
            "ABS_VOLLSTAENDIGKEIT", FEHLER, "Zuordnung",
            f"Interner Fehler: {n_in} Zeilen gelesen, aber {n_out} zugeordnet. Ausgabe wird nicht zugestellt.",
            "Bitte mit der Eingangsdatei als Fehler melden."))
    return list(sections.values()), findings
