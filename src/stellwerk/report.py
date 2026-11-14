"""Prüfbericht für die Person, die die Schnittstelle baut: Konsole, HTML, JSON.

Aufbau immer gleich: Was ist das Ergebnis? Was muss ich prüfen und was tue ich dann?
Wo steht was? Was wurde automatisch erledigt? Wo liegen die Dateien?
"""
from __future__ import annotations

import re
from html import escape

from . import __version__
from .model import FEHLER, INFO, PRUEFEN
from .pdftext import _ICD_VERLESEN

STATUS = {"ok": "OK", "pruefen": "PRÜFEN", "fehler": "FEHLER"}
STATUS_SATZ = {"ok": "Nichts zu prüfen. Das Bundle kann zugestellt werden.",
               "pruefen": "Das Bundle ist gebaut. Vor der Zustellung bitte die Punkte unten ansehen.",
               "fehler": "Es gibt kein zustellbares Bundle. Die Fehler unten müssen zuerst behoben werden."}
METHODE = {"quelle": "von der Quelle vorgegeben", "ueberschrift": "Überschrift erkannt",
           "unscharf": "Überschrift ähnlich geschrieben", "ki": "Vorschlag des lokalen Modells",
           "position": "nach Position im Text", "rest": "nicht zugeordnet"}
_UNSICHER = {"unscharf", "ki", "rest"}


def _kopfzeile(res) -> str:
    m = res.doc.meta
    parts = [res.tpl.titel, f"Patient {m.patient_id or '?'}"]
    if m.encounter_id:
        parts.append(f"Fall {m.encounter_id}")
    if m.doc_date:
        parts.append(f"{m.doc_date[8:10]}.{m.doc_date[5:7]}.{m.doc_date[:4]}")
    return " · ".join(parts)


def _quelle(res) -> str:
    d = res.doc
    art = {"hl7": f"HL7 {d.stats.get('nachrichtentyp', '')}".strip(), "pdf": "PDF", "sql": "SQL/CSV", "text": "Text"}[d.kind]
    extra = []
    if d.stats.get("seiten"):
        extra.append(f"{d.stats['seiten']} Seiten")
    if d.stats.get("ocr_seiten"):
        extra.append(f"Texterkennung {d.stats.get('ocr_sicherheit', 0):.0f} %")
    return f"{d.name} ({', '.join([art] + extra)})"


def _sec_rows(res):
    for s in res.sections:
        methods = s.methods
        unsure = bool(set(methods) & _UNSICHER) and not (s.key == res.tpl.kopf and methods == ["position"])
        label = ", ".join(METHODE[x] for x in methods)
        n = sum(1 for ln in s.lines if ln.text.strip())
        yield s, unsure, label, n


def to_json(res) -> dict:
    m = res.doc.meta
    return {
        "stellwerk": __version__, "status": res.status, "quelle": res.doc.name, "art": res.doc.kind,
        "vorlage": res.tpl.name, "modell": res.llm, "zustellung": res.delivery, "zugestellt": res.delivered,
        "dokument": {"patient_id": m.patient_id, "fallnummer": m.encounter_id, "dokument_id": m.doc_id,
                     "datum": m.doc_date, "titel": m.title, "autor": m.author},
        "kennzahlen": {k: v for k, v in res.doc.stats.items() if k != "eingang"},
        "abschnitte": [{"key": s.key, "titel": s.title, "zuordnung": s.methods, "zeilen": n,
                        "ocr_sicherheit": round(s.ocr_conf, 1) if s.ocr_conf is not None else None,
                        "ueberschriften_im_original": [p.heading for p in s.parts if p.heading]}
                       for s, _, _, n in _sec_rows(res)],
        "befunde": [{"code": f.code, "stufe": f.severity, "wo": f.where, "meldung": f.message,
                     "was_tun": f.action, **({"detail": f.detail} if f.detail else {})} for f in res.findings],
        "dateien": res.files,
    }


def _wrap(text: str, indent: str, width: int = 96) -> str:
    out, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width - len(indent):
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    out.append(line)
    return ("\n" + indent).join(out)


def render_text(res, skip: set | None = None) -> str:
    """skip: Befunde (code, meldung), die schon gemeinsam für alle Dokumente gezeigt wurden."""
    alle = [f for f in res.findings if not skip or (f.code, f.message) not in skip]
    L = [f"{STATUS[res.status]:<7} {_kopfzeile(res)}",
         f"        {STATUS_SATZ[res.status]}",
         f"        Quelle: {_quelle(res)} · Vorlage: {res.tpl.name} · Modell: {res.llm or 'keins'}", ""]
    for sev, title in ((FEHLER, "Fehler"), (PRUEFEN, "Zu prüfen")):
        items = [f for f in alle if f.severity == sev]
        if items:
            L.append(f"{title} ({len(items)})")
            for i, f in enumerate(items, 1):
                L.append(f"  {i}. {_wrap(f'[{f.where}] {f.message}' if f.where else f.message, '     ')}")
                if f.action:
                    L.append(f"     → {_wrap(f.action, '       ')}")
            L.append("")
    if res.sections:
        L.append("Abschnitte")
        for s, unsure, label, n in _sec_rows(res):
            ocr = f" · Texterkennung {s.ocr_conf:.0f} %" if s.ocr_conf is not None else ""
            L.append(f"  {'!' if unsure else '✓'} {s.title[:34]:<34} {n:>3} {'Zeile ' if n == 1 else 'Zeilen'} · {label}{ocr}")
        L.append("")
    infos = [f for f in alle if f.severity == INFO]
    if infos:
        L.append(f"Automatisch erledigt ({len(infos)})")
        L += [f"  · {_wrap(f'[{f.where}] {f.message}' if f.where else f.message, '    ')}" for f in infos]
        L.append("")
    if not res.tpl.eigen and any(f.detail.get("alias") for f in res.findings):
        L += [f"Vorlage anpassen: stellwerk vorlage {res.tpl.name} > {res.tpl.name}.yaml, Aliase ergänzen, dann mit",
              f"  --vorlage {res.tpl.name}.yaml aufrufen oder in stellwerk.yaml eintragen (vorlage: {res.tpl.name}.yaml).", ""]
    if res.files or res.delivery:
        L.append("Ausgabe")
        if res.files.get("bundle"):
            L.append(f"  Bundle     {res.files['bundle']}")
        if res.files.get("bericht"):
            L.append(f"  Bericht    {res.files['bericht']}")
        if res.delivery:
            L.append(f"  Zustellung {res.delivery}")
    return "\n".join(L).rstrip() + "\n"


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

_CSS = """
:root{--bg:#FAFAFA;--panel:#FFFFFF;--sub:#F4F4F5;--line:#E4E4E7;--focus:#A1A1AA;--ink:#09090B;--mut:#52525B;--dim:#A1A1AA;
--acc:#1D4ED8;--ok:#059669;--okbg:#ECFDF5;--okln:#A7F3D0;--warn:#B45309;--warnbg:#FFFBEB;--warnln:#FDE68A;
--err:#BE123C;--errbg:#FFF1F2;--errln:#FECDD3;--ai:#4338CA;--aibg:#EEF2FF;--ailn:#C7D2FE;
--sans:Geist,Inter,system-ui,-apple-system,"Segoe UI",sans-serif;--mono:"JetBrains Mono","Geist Mono",ui-monospace,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#09090B;--panel:#111113;--sub:#18181B;--line:#27272A;--focus:#52525B;--ink:#FAFAFA;
--mut:#A1A1AA;--dim:#52525B;--acc:#3B82F6;--ok:#34D399;--okbg:#06231A;--okln:#065F46;--warn:#FBBF24;--warnbg:#2A1D05;--warnln:#78350F;
--err:#FB7185;--errbg:#2B0A12;--errln:#881337;--ai:#A5B4FC;--aibg:#15173A;--ailn:#3730A3}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 var(--sans);font-feature-settings:"cv02","cv03","cv04","cv11"}
main{max-width:1180px;margin:0 auto;padding:16px}
header,section{background:var(--panel);border:1px solid var(--line);border-radius:6px;margin-bottom:12px;overflow:hidden}
header{padding:14px 16px}section>h2,section>details>summary{margin:0;padding:8px 16px;background:var(--sub);border-bottom:1px solid var(--line);
font-size:13px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--mut)}
section>details>summary{cursor:pointer}section>details:not([open])>summary{border-bottom:0}
section>details>summary:hover{color:var(--ink)}summary:focus-visible{outline:2px solid var(--focus);outline-offset:-2px}
.pad{padding:12px 16px}h1{font-size:19px;font-weight:600;letter-spacing:-.02em;margin:0 0 2px}
p{margin:3px 0}.mut{color:var(--mut)}.top{display:flex;flex-wrap:wrap;gap:10px 14px;align-items:flex-start}
.badge,.tag{display:inline-block;border:1px solid;border-radius:4px;font-weight:600;white-space:nowrap}
.badge{padding:5px 12px;font-size:13px;letter-spacing:.06em}.tag{padding:0 6px;font-size:12px;line-height:18px}
.ok{color:var(--ok);background:var(--okbg);border-color:var(--okln)}.pruefen{color:var(--warn);background:var(--warnbg);border-color:var(--warnln)}
.fehler{color:var(--err);background:var(--errbg);border-color:var(--errln)}.ki{color:var(--ai);background:var(--aibg);border-color:var(--ailn)}
ol.todo{margin:0;padding:0;list-style:none;counter-reset:n}
ol.todo li{counter-increment:n;display:grid;grid-template-columns:28px 1fr;gap:2px 8px;padding:10px 16px;border-top:1px solid var(--line)}
ol.todo li:first-child{border-top:0}ol.todo li::before{content:counter(n);color:var(--dim);font:12px/22px var(--mono);font-variant-numeric:tabular-nums}
.wo{font:500 13px var(--mono)}.tun{margin-top:4px;padding:5px 9px;border-left:2px solid var(--acc);background:var(--sub)}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%}
th{position:sticky;top:0;text-align:left;font-size:12px;font-weight:600;color:var(--mut);background:var(--sub);padding:0 12px;height:32px;
border-bottom:1px solid var(--line);white-space:nowrap}td{padding:6px 12px;border-top:1px solid var(--line);vertical-align:top}
tbody tr:hover td,table.src tr:hover td{background:var(--sub)}td.num{text-align:right;white-space:nowrap;font:13px var(--mono);font-variant-numeric:tabular-nums}
.src td{font:13px/1.45 var(--mono);border-top:0;padding:1px 12px;white-space:pre-wrap;overflow-wrap:anywhere}
.src tr.grp td{font:600 13px var(--sans);padding:7px 12px;background:var(--sub);border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.src tr.low td{background:var(--warnbg)}.src td.o{color:var(--dim);white-space:nowrap;font-size:12px;font-variant-numeric:tabular-nums;text-align:right}
mark{background:transparent;color:var(--warn);border-bottom:2px solid var(--warn);font-weight:600}
ul.done{margin:0;padding:10px 16px 10px 32px}ul.done li{margin:3px 0}
dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;margin:0}dt{color:var(--mut)}dd{margin:0;overflow-wrap:anywhere}
code{font:13px var(--mono)}
"""


def _e(s) -> str:
    return escape(str(s), quote=False)


def _line_html(ln, low: float) -> str:
    text = _e(ln.text)
    for w in sorted(set(ln.uncertain), key=len, reverse=True):
        if len(w) >= 2:
            text = re.sub(r"(?<![\w>])" + re.escape(_e(w)) + r"(?![\w<])", lambda m: f"<mark>{m.group(0)}</mark>", text)
    return text


def render_html(res) -> str:
    m = res.doc.meta
    st = res.status
    limit = 80
    h = ["<!doctype html><html lang='de'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
         f"<title>Prüfbericht {_e(m.doc_id or res.doc.name)}</title><style>{_CSS}</style></head><body><main>"]
    h.append(f"<header><div class='top'><span class='badge {st}'>{STATUS[st]}</span><div><h1>{_e(_kopfzeile(res))}</h1>"
             f"<p>{STATUS_SATZ[st]}</p><p class='mut'>Quelle: {_e(_quelle(res))} · Vorlage: {_e(res.tpl.name)} · "
             f"Lokales Modell: {_e(res.llm or 'keins')}</p>"
             + (f"<p class='mut'>Zustellung: {_e(res.delivery)}</p>" if res.delivery else "") + "</div></div></header>")

    todo = [f for f in res.findings if f.severity == FEHLER] + [f for f in res.findings if f.severity == PRUEFEN]
    if todo:
        h.append(f"<section><h2>Zu erledigen ({len(todo)})</h2><ol class='todo'>")
        for f in todo:
            art = "Fehler" if f.severity == FEHLER else "Prüfen"
            h.append(f"<li><div><span class='tag {f.severity}'>{art}</span> "
                     f"<span class='wo'>{_e(f.where)}</span><p>{_e(f.message)}</p>"
                     + (f"<p class='tun'><b>Was tun:</b> {_e(f.action)}</p>" if f.action else "") + "</div></li>")
        h.append("</ol></section>")

    if res.sections:
        h.append("<section><h2>Abschnitte im FHIR-Dokument</h2><div class='scroll'><table><thead><tr><th>Composition.section</th>"
                 "<th>Wie zugeordnet</th><th>Überschrift im Original</th><th>Zeilen</th><th>Texterkennung</th></tr></thead><tbody>")
        for s, unsure, label, n in _sec_rows(res):
            orig = ", ".join(p.heading for p in s.parts if p.heading) or "–"
            ocr = f"{s.ocr_conf:.0f} %" if s.ocr_conf is not None else "–"
            art = "ki" if "ki" in s.methods else "pruefen" if unsure else "ok"
            pill = f"<span class='tag {art}'>{'Modell' if art == 'ki' else 'prüfen' if unsure else 'sicher'}</span>"
            h.append(f"<tr><td><b>{_e(s.title)}</b></td><td>{pill} {_e(label)}</td><td>{_e(orig)}</td>"
                     f"<td class='num'>{n}</td><td class='num'>{ocr}</td></tr>")
        fehlend = [a["titel"] for a in res.tpl.abschnitte if not a.get("rolle") and a["key"] not in {s.key for s in res.sections}]
        h.append("</tbody></table></div>")
        if fehlend:
            h.append(f"<p class='mut pad'>Im Dokument nicht vorhanden: {_e(', '.join(fehlend))}.</p>")
        h.append("</section>")

        h.append("<section><h2>Zuordnung Zeile für Zeile</h2><p class='mut pad'>Der Text steht wörtlich so im FHIR-Dokument. "
                 "Hervorgehoben: von der Texterkennung unsicher gelesen, bitte mit dem Original vergleichen.</p>"
                 "<div class='scroll'><table class='src'>")
        for s in res.sections:
            for p in s.parts:
                head = _e(s.title) + (f" <span class='mut'>(im Original: {_e(p.heading)})</span>" if p.heading else "")
                h.append(f"<tr class='grp'><td colspan='2'>{head} <span class='mut'>· {METHODE[p.method]}</span></td></tr>")
                for ln in p.lines:
                    low = ln.conf is not None and (ln.conf < limit or ln.uncertain or _ICD_VERLESEN.search(ln.text)) and ln.text.strip()
                    info = (ln.origin + (f" · {ln.conf:.0f} %" if ln.conf is not None else "")) if ln.text.strip() else ""
                    cls = " class='low'" if low else ""
                    h.append(f"<tr{cls}><td>{_line_html(ln, limit) or '&#160;'}</td>"
                             f"<td class='o'>{_e(info)}</td></tr>")
        h.append("</table></div></section>")

    infos = [f for f in res.findings if f.severity == INFO]
    if infos:
        h.append(f"<section><details><summary>Automatisch erledigt ({len(infos)})</summary><ul class='done'>")
        h += [f"<li><span class='wo'>{_e(f.where)}</span>: {_e(f.message)}"
              + (f" <span class='mut'>{_e(f.action)}</span>" if f.action else "") + "</li>" for f in infos]
        h.append("</ul></details></section>")

    h.append("<section><h2>Dateien</h2><div class='pad'><dl>")
    for key, label in (("bundle", "FHIR-Bundle"), ("original", "Original (unverändert)"), ("original_sha256", "SHA-256 des Originals"),
                       ("text", "Gelesener Text"), ("bericht_json", "Bericht als JSON")):
        if res.files.get(key):
            h.append(f"<dt>{label}</dt><dd><code>{_e(res.files[key])}</code></dd>")
    h.append(f"</dl><p class='mut'>Stellwerk {__version__}. Dieser Bericht enthält Patientendaten und gehört nicht in ein Ticket oder Repository.</p></div></section>")
    h.append("</main></body></html>")
    return "".join(h)
