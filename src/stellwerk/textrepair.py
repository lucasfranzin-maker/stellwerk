"""Zeichensätze erkennen, Text reparieren und normalisieren.

Grundsatz: Es wird nur repariert, was sich verlustfrei und eindeutig
zurückrechnen lässt. Alles andere wird gemeldet und bleibt unverändert.
Jede Änderung erscheint als Befund mit Anzahl und Beispielen.
"""
from __future__ import annotations

import codecs
import re
import unicodedata
from html.parser import HTMLParser

from .model import INFO, PRUEFEN, Finding, zahl

# --------------------------------------------------------------------------
# Bytes -> Text
# --------------------------------------------------------------------------

_DECLARED = {
    "8859/1": "latin-1", "ISO-8859-1": "latin-1", "ISO8859-1": "latin-1", "LATIN-1": "latin-1", "LATIN1": "latin-1",
    "8859/15": "iso-8859-15", "ISO-8859-15": "iso-8859-15",
    "8859/2": "iso-8859-2",
    "UNICODE UTF-8": "utf-8", "UTF-8": "utf-8", "UTF8": "utf-8", "UNICODE": "utf-8",
    "UNICODE UTF-16": "utf-16", "UTF-16": "utf-16",
    "ASCII": "ascii", "US-ASCII": "ascii",
    "WINDOWS-1252": "cp1252", "CP1252": "cp1252", "1252": "cp1252",
    "IBM850": "cp850", "CP850": "cp850", "850": "cp850",
}
_NAMES = {"utf-8": "UTF-8", "cp1252": "Windows-1252", "latin-1": "ISO 8859-1",
          "iso-8859-15": "ISO 8859-15", "cp850": "IBM850 (DOS)", "utf-16": "UTF-16",
          "ascii": "ASCII", "iso-8859-2": "ISO 8859-2"}


def _c1_fallback(err):  # Bytes, die Windows-1252 nicht kennt, wie Latin-1 lesen
    return err.object[err.start:err.end].decode("latin-1"), err.end


codecs.register_error("stellwerk-c1", _c1_fallback)

_TYPISCH = set("äöüÄÖÜßéèêàáâôûîçñÉ§°µ²³€–—„“”‚‘’«»…·•½¼¾±×´`")
_IM_WORT_VERDAECHTIG = set("„“”‚‘’™¢£¥¤¦¨©ª¬®¯¸¹º¿÷×")


def _badness(s: str) -> int:
    """Wie unplausibel sieht dieser Text für deutschsprachige Klinikdaten aus?"""
    bad = 0
    n = len(s)
    for i, ch in enumerate(s):
        o = ord(ch)
        if o < 128:
            continue
        if 0x80 <= o <= 0x9F or 0x2500 <= o <= 0x259F:       # C1-Steuerzeichen, Rahmenzeichen
            bad += 3
            continue
        between = 0 < i < n - 1 and s[i - 1].isalpha() and s[i + 1].isalpha()
        if ch in _IM_WORT_VERDAECHTIG and between:
            bad += 3
        elif ch in _TYPISCH:
            continue
        elif ch.isalpha():
            bad += 1
        else:
            bad += 2
    return bad


def _single_byte(data: bytes, enc: str) -> str:
    if enc == "cp1252":
        return data.decode("cp1252", errors="stellwerk-c1")
    return data.decode(enc, errors="replace")


def canonical_charset(declared: str | None) -> str | None:
    if not declared:
        return None
    return _DECLARED.get(declared.strip().upper())


def decode_bytes(data: bytes, declared: str | None = None, where: str = "") -> tuple[str, list[Finding]]:
    """Bytes robust in Text wandeln. `declared` ist die Angabe der Quelle (z. B. MSH-18)."""
    findings: list[Finding] = []
    decl = canonical_charset(declared)
    decl_txt = f"„{declared}“" if declared else "nichts"

    if data.startswith(codecs.BOM_UTF8):
        data = data[3:]
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors="replace"), findings
    if decl == "utf-16":
        try:
            return data.decode("utf-16"), findings
        except UnicodeDecodeError:
            pass

    if all(b < 128 for b in data):
        return data.decode("ascii"), findings

    try:
        text = data.decode("utf-8")
        if decl and decl not in ("utf-8",):
            findings.append(Finding(
                "ENC_DEKLARIERT_FALSCH", INFO, where,
                f"Die Quelle nennt als Zeichensatz {decl_txt}, der Inhalt ist aber gültiges UTF-8. Als UTF-8 gelesen.",
                "Zeichensatzangabe im sendenden System korrigieren, sonst lesen andere Empfänger Umlaute falsch."))
        return text, findings
    except UnicodeDecodeError:
        pass

    # Kein durchgängiges UTF-8: zeilenweise entscheiden (gemischte Lieferungen kommen vor).
    lines = re.split(rb"(\r\n|\r|\n)", data)
    utf8_lines = 0
    other: list[int] = []
    decoded: list[str | None] = []
    for i, chunk in enumerate(lines):
        if all(b < 128 for b in chunk):
            decoded.append(chunk.decode("ascii"))
            continue
        try:
            decoded.append(chunk.decode("utf-8"))
            utf8_lines += 1
        except UnicodeDecodeError:
            decoded.append(None)
            other.append(i)

    sample = b"\n".join(lines[i] for i in other)
    candidates = []
    if decl and decl not in ("utf-8", "ascii", "utf-16"):
        candidates.append(decl)
    for enc in ("cp1252", "cp850"):
        if enc not in candidates and not (enc == "cp1252" and "latin-1" in candidates):
            candidates.append(enc)
    best = min(candidates, key=lambda e: (_badness(_single_byte(sample, e)), candidates.index(e)))
    for i in other:
        decoded[i] = _single_byte(lines[i], best)
    text = "".join(d or "" for d in decoded)

    name = _NAMES.get(best, best)
    if utf8_lines:
        findings.append(Finding(
            "ENC_GEMISCHT", PRUEFEN, where,
            f"Gemischte Zeichensätze in einer Lieferung: {zahl(utf8_lines, 'Zeile', 'Zeilen')} UTF-8, {zahl(len(other), 'Zeile', 'Zeilen')} {name}. "
            "Zeilenweise gelesen.",
            "Umlaute stichprobenartig prüfen. Ursache ist meist ein System, das Textbausteine ungeprüft zusammenfügt."))
    elif decl == best or (decl == "latin-1" and best == "cp1252"):
        pass                                    # Angabe stimmt, kein Befund nötig
    elif best == "cp850":
        findings.append(Finding(
            "ENC_GERATEN", PRUEFEN, where,
            f"Zeichensatz nicht verlässlich angegeben ({decl_txt}); der Inhalt passt am besten zu {name}.",
            "Umlaute im Ergebnis stichprobenartig prüfen und den Zeichensatz in der Schnittstelle fest einstellen."))
    else:
        findings.append(Finding(
            "ENC_ERKANNT", INFO, where,
            f"Die Quelle nennt als Zeichensatz {decl_txt}, der Inhalt ist kein gültiges UTF-8. Als {name} gelesen.",
            "" if not declared else "Zeichensatzangabe im sendenden System korrigieren."))
    return text, findings


# --------------------------------------------------------------------------
# Fehlkodierte Zeichenfolgen (Mojibake) zurückrechnen
# --------------------------------------------------------------------------

# UTF-8-Bytes, die als Windows-1252/Latin-1 gelesen wurden: ein Startzeichen
# (Ã, Â, â ...) gefolgt von 1 bis 3 Zeichen, die aus den Bytes 0x80-0xBF entstehen.
_CONT = "".join(sorted({bytes([b]).decode("cp1252", errors="stellwerk-c1") for b in range(0x80, 0xC0)}
                       | {chr(b) for b in range(0x80, 0xC0)}))
_MOJI = re.compile("[\u00C2-\u00F4][" + re.escape(_CONT) + "]{1,3}")


def _unmojibake(match: re.Match) -> str:
    s = match.group(0)
    raw = bytearray()
    for ch in s:
        try:
            raw += ch.encode("cp1252")
        except UnicodeEncodeError:
            if ord(ch) < 256:
                raw.append(ord(ch))
            else:
                return s
    # Längste gültige UTF-8-Folge ab Beginn nehmen, Rest unverändert lassen.
    for cut in range(len(raw), 1, -1):
        try:
            fixed = bytes(raw[:cut]).decode("utf-8")
        except UnicodeDecodeError:
            continue
        if len(fixed) == 1 and (fixed.isprintable() or fixed == "\u00a0"):
            return fixed + s[cut:]
    return s


def fix_mojibake(text: str) -> tuple[str, int, list[tuple[str, str]]]:
    """Gibt (Text, Anzahl Reparaturen, Beispiele) zurück. Bis zu drei Durchläufe für Mehrfachkodierung."""
    total, samples = 0, []
    for _ in range(3):
        count = 0

        def repl(m: re.Match) -> str:
            nonlocal count
            new = _unmojibake(m)
            if new != m.group(0):
                count += 1
                if len(samples) < 4 and (m.group(0), new) not in samples:
                    samples.append((m.group(0), new))
            return new

        text = _MOJI.sub(repl, text)
        if not count:
            break
        total += count
    return text, total, samples


# --------------------------------------------------------------------------
# Normalisieren
# --------------------------------------------------------------------------

_LIGATUREN = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl",
              "\ufb05": "st", "\ufb06": "st"}
_LEERZEICHEN = dict.fromkeys(map(chr, [0x00A0, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006,
                                       0x2007, 0x2008, 0x2009, 0x200A, 0x202F, 0x205F, 0x3000]), " ")
_UNSICHTBAR = set(map(chr, [0x00AD, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF]))
_QMARK = re.compile(r"[A-Za-zÄÖÜäöüß]+\?[a-zäöüß]+(?:\?[a-zäöüß]+)*")


def _context(text: str, pos: int, width: int = 14) -> str:
    return text[max(0, pos - width):pos + width + 1].replace("\n", " ").strip()


def clean_text(text: str, where: str = "", repair: bool = True) -> tuple[str, list[Finding]]:
    """Text reparieren und normalisieren. Kein NFKC: „m²“ und „µg“ bleiben, wie sie sind."""
    findings: list[Finding] = []

    if repair:
        text, n, samples = fix_mojibake(text)
        if n:
            bsp = ", ".join(f"„{a}“ → „{b}“" for a, b in samples)
            findings.append(Finding(
                "ENC_REPARIERT", INFO, where,
                f"{n} fehlkodierte Zeichenfolgen zurückgerechnet ({bsp}).",
                "Ursache liegt vor Stellwerk: UTF-8 wurde unterwegs als Windows-1252 gelesen. Dort beheben.",
                {"anzahl": n}))
        rest = [m.group(0) for m in _MOJI.finditer(text)]
        if rest:
            findings.append(Finding(
                "ENC_VERDAECHTIG", PRUEFEN, where,
                f"{len(rest)} Zeichenfolgen sehen fehlkodiert aus, lassen sich aber nicht eindeutig zurückrechnen "
                f"(z. B. „{rest[0]}“). Unverändert übernommen.",
                "Am Original prüfen."))

    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x0c", "\n").replace("\x0b", "\n")

    counts = {"steuer": 0, "leer": 0, "unsichtbar": 0, "ligatur": 0}
    out = []
    for ch in text:
        o = ord(ch)
        if ch in ("\n", "\t"):
            out.append(ch)
        elif o < 32 or 0x7F <= o <= 0x9F or 0xD800 <= o <= 0xDFFF or o in (0xFFFE, 0xFFFF):
            counts["steuer"] += 1
        elif ch in _LEERZEICHEN:
            counts["leer"] += 1
            out.append(" ")
        elif ch in _UNSICHTBAR:
            counts["unsichtbar"] += 1
        elif ch in _LIGATUREN:
            counts["ligatur"] += 1
            out.append(_LIGATUREN[ch])
        else:
            out.append(ch)
    text = "".join(out)

    nfc = unicodedata.normalize("NFC", text)
    zerlegt = len(text) - len(nfc)
    text = nfc

    teile = []
    if counts["steuer"]:
        teile.append(f"{counts['steuer']} Steuerzeichen entfernt")
    if counts["unsichtbar"]:
        teile.append(f"{counts['unsichtbar']} unsichtbare Zeichen entfernt")
    if counts["ligatur"]:
        teile.append(f"{counts['ligatur']} Ligaturen aufgelöst")
    if zerlegt > 0:
        teile.append(f"{zerlegt} zerlegte Umlaute zusammengesetzt")
    if teile:                                   # geschützte Leerzeichen allein sind keine Meldung wert
        findings.append(Finding("TXT_NORMALISIERT", INFO, where, "Sonderzeichen normalisiert: " + ", ".join(teile) + "."))

    lost = [i for i, ch in enumerate(text) if ch == "\ufffd"]
    if lost:
        bsp = "; ".join(f"„{_context(text, i)}“" for i in lost[:3])
        findings.append(Finding(
            "ENC_VERLOREN", PRUEFEN, where,
            f"{len(lost)} Zeichen sind schon in der Quelle zerstört (�) und nicht wiederherstellbar: {bsp}.",
            "Quelle korrigieren und neu liefern lassen, oder die Stellen am Original prüfen.",
            {"anzahl": len(lost)}))
    qm = _QMARK.findall(text)
    if qm:
        bsp = ", ".join(f"„{w}“" for w in list(dict.fromkeys(qm))[:4])
        findings.append(Finding(
            "ENC_FRAGEZEICHEN", PRUEFEN, where,
            f"{len(qm)} Wörter enthalten ein Fragezeichen mitten im Wort ({bsp}). "
            "Vermutlich wurden Umlaute schon in der Quelle durch „?“ ersetzt.",
            "Nicht automatisch reparierbar. Export des Quellsystems auf Zeichensatz prüfen.",
            {"anzahl": len(qm)}))
    return text, findings


# --------------------------------------------------------------------------
# RTF und HTML aus Datenbankfeldern
# --------------------------------------------------------------------------

_RTF_SKIP = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "header", "footer",
             "headerl", "headerr", "footerl", "footerr", "themedata", "datastore", "latentstyles",
             "generator", "xmlnstbl", "listtable", "listoverridetable", "rsidtbl", "filetbl", "revtbl"}
_RTF_BREAK = {"par": "\n", "line": "\n", "sect": "\n", "page": "\n", "row": "\n", "tab": "\t", "cell": "\t",
              "emdash": "—", "endash": "–", "bullet": "•", "lquote": "‘", "rquote": "’",
              "ldblquote": "“", "rdblquote": "”", "emspace": " ", "enspace": " "}
_RTF_TOKEN = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z])|([{}])|\r?\n|([^\\{}\r\n]+)")


def rtf_to_text(rtf: str) -> str:
    """RTF auf den reinen Text reduzieren. Formatierung, Schriften und Bilder entfallen."""
    codepage = "cp1252"
    out: list[str] = []
    stack: list[bool] = []          # True = aktuelle Gruppe wird übersprungen
    skip = False
    uc_skip = 1
    pending_skip = 0
    hexbuf = bytearray()

    def flush():
        if hexbuf:
            out.append(hexbuf.decode(codepage, errors="replace"))
            hexbuf.clear()

    for m in _RTF_TOKEN.finditer(rtf):
        word, arg, hexa, sym, brace, plain = m.groups()
        if brace == "{":
            flush()
            stack.append(skip)
        elif brace == "}":
            flush()
            skip = stack.pop() if stack else False
        elif word:
            flush()
            if word == "ansicpg" and arg:
                codepage = f"cp{arg}"
                try:
                    codecs.lookup(codepage)
                except LookupError:
                    codepage = "cp1252"
            elif word in _RTF_SKIP:
                skip = True
            elif word == "uc" and arg:
                uc_skip = int(arg)
            elif word == "u" and arg and not skip:
                cp = int(arg)
                out.append(chr(cp + 65536 if cp < 0 else cp))
                pending_skip = uc_skip
            elif word in _RTF_BREAK and not skip:
                out.append(_RTF_BREAK[word])
        elif hexa:
            if pending_skip:
                pending_skip -= 1
            elif not skip:
                hexbuf.append(int(hexa, 16))
        elif sym:
            flush()
            if sym == "*":
                skip = True
            elif not skip:
                out.append({"~": " ", "-": "", "_": "-"}.get(sym, sym))
        elif plain:
            flush()
            if pending_skip:
                plain, pending_skip = plain[pending_skip:], 0
            if not skip:
                out.append(plain)
    flush()
    return "".join(out).strip()


class _HTMLText(HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in self._BLOCK:
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append("\t")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK and tag != "br":
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _HTMLText()
    p.feed(html)
    p.close()
    text = "".join(p.out)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


_HTML_HINT = re.compile(r"<(p|div|br|span|html|body|table|b|i|font|ul|li)\b[^>]*>", re.I)


def strip_markup(text: str, where: str = "") -> tuple[str, list[Finding]]:
    """RTF oder HTML in Datenbankfeldern erkennen und auf Text reduzieren."""
    head = text.lstrip()[:20]
    if head.startswith("{\\rtf"):
        return rtf_to_text(text), [Finding("TXT_RTF", INFO, where, "Inhalt ist RTF. Auf reinen Text reduziert; Formatierung und Bilder entfallen.")]
    if len(_HTML_HINT.findall(text)) >= 2 or head.lower().startswith(("<html", "<!doctype", "<p", "<div")):
        return html_to_text(text), [Finding("TXT_HTML", INFO, where, "Inhalt ist HTML. Auf reinen Text reduziert.")]
    return text, []
