"""Text aus PDFs holen: Textebene, wenn sie taugt; sonst Texterkennung mit Sicherheitswerten."""
from __future__ import annotations

import io
import re
import shutil

from .model import FEHLER, INFO, PRUEFEN, Finding, Line
from .textrepair import clean_text

_VOKAL = re.compile(r"[aeiouyäöüAEIOUYÄÖÜ]")
_WORT = re.compile(r"[A-Za-zÄÖÜäöüß]{3,}")
_SEITENZAHL = re.compile(r"^\s*(?:[-–]\s*\d{1,3}\s*[-–]|seite\s+\d{1,3}(?:\s*(?:von|/)\s*\d{1,3})?|\d{1,3}\s*/\s*\d{1,3})\s*$", re.I)


# Sieht aus wie ein ICD-Code, dessen Buchstabe als Ziffer gelesen wurde: (110.90) statt (I10.90)
_ICD_VERLESEN = re.compile(r"\((?:\d{2}\.\d{1,2}|[1l|]\d{2}\.\d{1,2})\)")


def text_quality(text: str) -> tuple[bool, str]:
    """Taugt die Textebene einer Seite? Liefert (ja/nein, Begründung)."""
    chars = [c for c in text if not c.isspace()]
    if len(chars) < 20:
        return False, "kein Text"
    weird = sum(1 for c in chars if c == "�" or 0xE000 <= ord(c) <= 0xF8FF or ord(c) < 32)
    weird += 5 * len(re.findall(r"\(cid:\d+\)", text))
    if weird / len(chars) > 0.02:
        return False, "Textebene enthält unlesbare Zeichen"
    words = _WORT.findall(text)
    if len(words) >= 5 and sum(1 for w in words if _VOKAL.search(w)) / len(words) < 0.85:
        return False, "Textebene ergibt keine lesbaren Wörter"
    return True, ""


def ocr_available(lang: str = "deu") -> tuple[bool, str]:
    if not shutil.which("tesseract"):
        return False, "Das Programm tesseract ist nicht installiert."
    if not shutil.which("pdftoppm"):
        return False, "Das Programm pdftoppm (poppler) ist nicht installiert."
    try:
        import pdf2image  # noqa: F401
        import pytesseract
    except ImportError:
        return False, "Die Python-Pakete für Texterkennung fehlen (pip install 'stellwerk[ocr]')."
    try:
        langs = pytesseract.get_languages(config="")
    except Exception as exc:                                    # pragma: no cover
        return False, f"tesseract lässt sich nicht starten: {exc}"
    missing = [x for x in lang.split("+") if x not in langs]
    if missing:
        return False, f"tesseract fehlt das Sprachpaket {', '.join(missing)}."
    return True, ""


def _deskew(img):
    """Leichte Schräglage (bis 5 Grad) über das Zeilenprofil ausgleichen."""
    try:
        import numpy as np
    except ImportError:
        return img, 0.0
    from PIL import Image
    small = img.convert("L")
    small.thumbnail((700, 700))
    best, best_score = 0.0, -1.0
    for step in range(-20, 21):
        angle = step * 0.25
        rot = small.rotate(angle, resample=Image.BILINEAR, fillcolor=255)
        arr = np.asarray(rot) < 140
        prof = arr.sum(axis=1).astype(float)
        score = float(((prof[1:] - prof[:-1]) ** 2).sum())
        if score > best_score:
            best, best_score = angle, score
    if abs(best) < 0.4:
        return img, 0.0
    return img.rotate(best, resample=Image.BICUBIC, expand=True, fillcolor=255), best


def _ocr_page(data: bytes, page: int, cfg: dict) -> tuple[list[Line], dict]:
    import pytesseract
    from pdf2image import convert_from_bytes
    from PIL import ImageOps

    ocr = cfg.get("ocr", {})
    lang = ocr.get("sprache", "deu")
    low = ocr.get("wort_unsicher_unter", 60)
    img = convert_from_bytes(data, dpi=ocr.get("dpi", 300), first_page=page, last_page=page, grayscale=True)[0]
    info: dict = {"gedreht": 0, "schraeg": 0.0, "entrauscht": False}
    try:
        osd = pytesseract.image_to_osd(img, output_type=pytesseract.Output.DICT)
        if osd.get("rotate") in (90, 180, 270) and float(osd.get("orientation_conf", 0)) >= 2.0:
            img = img.rotate(-osd["rotate"], expand=True, fillcolor=255)
            info["gedreht"] = osd["rotate"]
    except Exception:
        pass                                    # zu wenig Text für eine Lageerkennung
    img, info["schraeg"] = _deskew(img)
    img = ImageOps.autocontrast(img, cutoff=1)
    def read(im):
        data = pytesseract.image_to_data(im, lang=lang, output_type=pytesseract.Output.DICT)
        ws = [(len(w), float(c)) for w, c in zip(data["text"], data["conf"]) if w.strip() and float(c) >= 0]
        return data, (sum(n * c for n, c in ws) / max(sum(n for n, _ in ws), 1) if ws else 0.0)

    d, mean = read(img)
    info["entrauscht"] = False
    if mean < 80:                               # zweiter Versuch: Flecken und Rauschen herausfiltern
        from PIL import ImageFilter
        d2, mean2 = read(img.filter(ImageFilter.MedianFilter(5)))
        if mean2 > mean + 3:
            d, info["entrauscht"] = d2, True

    lines: list[Line] = []
    cur_key, cur_par, words = None, None, []

    def flush():
        if not words:
            return
        text = " ".join(w for w, _ in words)
        weight = sum(len(w) for w, _ in words)
        conf = sum(len(w) * c for w, c in words) / max(weight, 1)
        lines.append(Line(text, f"Seite {page}", conf=round(conf, 1), uncertain=[w for w, c in words if c < low]))

    for i, w in enumerate(d["text"]):
        if not w.strip() or float(d["conf"][i]) < 0:
            continue
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        if key != cur_key:
            flush()
            words = []
            if cur_par is not None and key[:2] != cur_par:
                lines.append(Line("", f"Seite {page}"))
            cur_key, cur_par = key, key[:2]
        words.append((w, float(d["conf"][i])))
    flush()
    return lines, info


def pdf_to_lines(data: bytes, cfg: dict, where: str = "PDF") -> tuple[list[Line], list[Finding], dict]:
    """Liefert Zeilen mit Seitenangabe und OCR-Sicherheit, Befunde und Kennzahlen."""
    from pypdf import PdfReader

    ocr_cfg = cfg.get("ocr", {})
    mode = ocr_cfg.get("modus", "auto")
    findings: list[Finding] = []
    stats: dict = {"seiten": 0, "ocr_seiten": 0, "ocr_sicherheit": None}
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("passwortgeschützt")
        n_pages = len(reader.pages)
    except Exception as exc:
        findings.append(Finding("PDF_UNLESBAR", FEHLER, where, f"Das PDF lässt sich nicht öffnen ({exc}).",
                                "Datei prüfen. Passwortgeschützte PDFs vorher entsperren."))
        return [], findings, stats
    stats["seiten"] = n_pages

    pages: list[list[Line]] = []
    ocr_pages, bad_layer, confs, rotated, skewed, denoised = [], [], [], [], [], []
    ocr_ok, ocr_why = (None, "")
    for p in range(1, n_pages + 1):
        try:
            layer = reader.pages[p - 1].extract_text() or ""
        except Exception:
            layer = ""
        good, why = text_quality(layer)
        if mode == "immer" or (mode == "auto" and not good):
            if ocr_ok is None:
                ocr_ok, ocr_why = ocr_available(ocr_cfg.get("sprache", "deu"))
            if ocr_ok:
                lines, info = _ocr_page(data, p, cfg)
                for ln in lines:
                    ln.text, _ = clean_text(ln.text, repair=False)
                ocr_pages.append(p)
                if layer.strip() and not good:
                    bad_layer.append(p)
                if info["gedreht"]:
                    rotated.append(p)
                if info["schraeg"]:
                    skewed.append(p)
                if info["entrauscht"]:
                    denoised.append(p)
                vals = [(ln.conf, len(ln.text)) for ln in lines if ln.conf is not None]
                if vals:
                    confs.append((p, sum(c * w for c, w in vals) / max(sum(w for _, w in vals), 1)))
                pages.append(lines)
                continue
            if not good:
                findings.append(Finding(
                    "PDF_OCR_FEHLT", FEHLER, f"{where}, Seite {p}",
                    f"Die Seite enthält keinen brauchbaren Text ({why}) und Texterkennung steht nicht zur Verfügung. {ocr_why}",
                    "Unter Debian/Ubuntu: apt install tesseract-ocr tesseract-ocr-deu poppler-utils; pip install 'stellwerk[ocr]'."))
        elif not good:
            findings.append(Finding("PDF_KEIN_TEXT", PRUEFEN, f"{where}, Seite {p}",
                                    f"Seite ohne brauchbaren Text ({why}); Texterkennung ist abgeschaltet.",
                                    "ocr.modus auf auto stellen."))
        text, f = clean_text(layer, f"{where}, Seite {p}")
        findings += f
        pages.append([Line(x.rstrip(), f"Seite {p}") for x in text.split("\n")])

    # --- wiederkehrende Kopf- und Fußzeilen, Seitenzahlen
    def key(s: str) -> str:
        return re.sub(r"\d", "#", re.sub(r"\s+", " ", s.strip().lower()))

    seen: dict[str, set[int]] = {}
    for i, lines in enumerate(pages):
        filled = [ln for ln in lines if ln.text.strip()]
        for ln in filled[:4] + filled[-4:]:
            seen.setdefault(key(ln.text), set()).add(i)
    running = {k for k, v in seen.items() if len(v) >= 2 and len(v) >= 0.6 * n_pages} if n_pages > 1 else set()
    out: list[Line] = []
    dropped: list[str] = []
    for i, lines in enumerate(pages):
        for ln in lines:
            k = key(ln.text)
            kompakt = re.sub(r"\s+", "", ln.text.lower())
            if ln.text.strip() and (_SEITENZAHL.match(ln.text) or re.fullmatch(r"seite[\dil|]{1,4}(von|/)\W?[\dil|]{1,3}", kompakt)
                                    or (k in running and i > 0)):
                dropped.append(ln.text.strip())
                continue
            out.append(ln)
        if i < n_pages - 1:
            out.append(Line("", f"Seite {i + 1}"))
    if dropped:
        uniq = list(dict.fromkeys(dropped))
        findings.append(Finding(
            "PDF_KOPF_FUSS", INFO, where,
            f"{len(dropped)} wiederkehrende Kopf-/Fußzeilen und Seitenzahlen ausgelassen: "
            + ", ".join(f"„{u}“" for u in uniq[:4]) + (" …" if len(uniq) > 4 else "") + ".",
            detail={"zeilen": uniq}))

    # --- Befunde zur Texterkennung
    if ocr_pages:
        mean = sum(c for _, c in confs) / len(confs) if confs else 0.0
        stats.update(ocr_seiten=len(ocr_pages), ocr_sicherheit=round(mean, 1))
        extra = []
        if bad_layer:
            extra.append(f"Seite {', '.join(map(str, bad_layer))} hatte eine unbrauchbare Textebene")
        if rotated:
            extra.append(f"Seite {', '.join(map(str, rotated))} gedreht")
        if skewed:
            extra.append(f"Seite {', '.join(map(str, skewed))} begradigt")
        if denoised:
            extra.append(f"Seite {', '.join(map(str, denoised))} entrauscht")
        findings.append(Finding(
            "PDF_OCR", INFO, where,
            f"Texterkennung auf {len(ocr_pages)} von {n_pages} Seiten, mittlere Sicherheit {mean:.0f} %"
            + (" (" + "; ".join(extra) + ")" if extra else "") + ".",
            detail={"seiten": ocr_pages}))
        for p, c in confs:
            if c < ocr_cfg.get("seite_warnen_unter", 75):
                findings.append(Finding(
                    "PDF_OCR_SCHLECHT", PRUEFEN, f"{where}, Seite {p}",
                    f"Die Seite ist schlecht lesbar (Erkennungssicherheit {c:.0f} %).",
                    "Text der Seite am Original gegenlesen oder besser scannen lassen (300 dpi, gerade, Graustufen)."))
        limit = ocr_cfg.get("zahl_warnen_unter", 80)
        low = ocr_cfg.get("wort_unsicher_unter", 60)
        risky = [ln for ln in out if ln.conf is not None and re.search(r"\d", ln.text)
                 and (ln.conf < limit or any(re.search(r"\d", w) for w in ln.uncertain) or _ICD_VERLESEN.search(ln.text))]
        if risky:
            bsp = "; ".join(f"{ln.origin}: „{ln.text[:70]}“ ({ln.conf:.0f} %)" for ln in risky[:5])
            findings.append(Finding(
                "PDF_OCR_ZAHL", PRUEFEN, where,
                f"{len(risky)} Zeilen mit Zahlen sind unsicher erkannt. Dosierungen, Laborwerte und Daten können falsch sein: {bsp}"
                + (" …" if len(risky) > 5 else ""),
                "Diese Zeilen am Original prüfen. Im HTML-Bericht sind sie markiert.",
                {"anzahl": len(risky), "schwelle": limit, "wortschwelle": low}))
    return out, findings, stats
