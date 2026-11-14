"""Kleiner HTTP-Dienst: Datei hinschicken, Bundle und Prüfbericht zurückbekommen.

Für den Aufruf aus dem Kommunikationsserver (POST /v1/convert) und als Seite zum Ausprobieren.
Ohne Anmeldung: nur an localhost binden oder hinter einen Reverse-Proxy stellen.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from importlib import resources

from . import __version__, workbench
from .fieldmap import dump_json
from .pipeline import deliver, ingest, make_llm, process, write_outputs
from .report import render_html, to_json
from .sections import list_templates

MAX_BYTES = 50 * 1024 * 1024

_PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stellwerk</title><style>
:root{--bg:#FAFAFA;--panel:#FFF;--sub:#F4F4F5;--line:#E4E4E7;--focus:#A1A1AA;--ink:#09090B;--mut:#52525B;--acc:#1D4ED8}
@media (prefers-color-scheme:dark){:root{--bg:#09090B;--panel:#111113;--sub:#18181B;--line:#27272A;--focus:#52525B;--ink:#FAFAFA;--mut:#A1A1AA;--acc:#3B82F6}}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 Geist,Inter,system-ui,sans-serif;background:var(--bg);color:var(--ink)}
main{max-width:1180px;margin:0 auto;padding:16px;display:flex;flex-direction:column;gap:12px}h1{font-size:19px;font-weight:600;letter-spacing:-.02em;margin:0}
form{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:12px 16px;display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end}
label{display:flex;flex-direction:column;gap:3px;font-size:13px;font-weight:500;color:var(--mut);flex:1 1 150px}
input,select{font:13px "JetBrains Mono","Geist Mono",ui-monospace,monospace;padding:0 8px;height:36px;border:1px solid var(--line);border-radius:4px;background:var(--panel);color:var(--ink)}
input:hover,select:hover{border-color:var(--focus)}input:focus-visible,select:focus-visible,button:focus-visible{outline:2px solid var(--acc);outline-offset:1px}
button{font:inherit;font-weight:600;height:36px;padding:0 16px;border-radius:4px;border:1px solid var(--acc);background:var(--acc);color:#fff;cursor:pointer}
button:hover{filter:brightness(1.1)}button:disabled{opacity:.5;cursor:default}
iframe{width:100%;height:78vh;border:1px solid var(--line);border-radius:6px;background:var(--panel)}p{margin:0;color:var(--mut)}
</style></head><body><main><h1>Stellwerk</h1>
<p>HL7-Nachricht, PDF oder Text hochladen. Zurück kommen FHIR-Bundle und Prüfbericht. Die Datei bleibt auf diesem Rechner.</p>
<form id="f"><label style="flex:2 1 260px">Datei<input type="file" id="datei" required></label>
<label>Vorlage<select id="vorlage"><option value="">automatisch</option>__VORLAGEN__</select></label>
<label>Patientennummer<input id="patient_id" placeholder="nur bei PDF/Text"></label>
<label>Fallnummer<input id="fall" placeholder="nur bei PDF/Text"></label>
<label>Dokumentnummer<input id="dokument_id" placeholder="nur bei PDF/Text"></label>
<button>Umwandeln</button></form><p id="s" role="status"></p><iframe id="r" title="Prüfbericht" hidden></iframe></main>
<script>
document.getElementById('f').addEventListener('submit', async e => {
  e.preventDefault(); const s = document.getElementById('s'), r = document.getElementById('r');
  const file = document.getElementById('datei').files[0]; const q = new URLSearchParams({name: file.name});
  for (const k of ['vorlage','patient_id','fall','dokument_id']) { const v = document.getElementById(k).value; if (v) q.set(k, v); }
  s.textContent = 'Wird verarbeitet … (gescannte Seiten brauchen einige Sekunden)';
  try { const resp = await fetch('/v1/convert?' + q, {method:'POST', headers:{'Accept':'text/html'}, body: file});
    r.srcdoc = await resp.text(); r.hidden = false; s.textContent = ''; }
  catch (err) { s.textContent = 'Fehlgeschlagen: ' + err; }
});
</script></body></html>"""


def make_handler(cfg: dict, outdir: str):
    ws = workbench.Workspace(cfg.get("arbeitsbereich") or "arbeitsbereich")

    class Handler(BaseHTTPRequestHandler):
        server_version = f"Stellwerk/{__version__}"

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: dict):
            self._send(code, dump_json(obj).encode("utf-8"), "application/json; charset=utf-8")

        def log_message(self, fmt, *args):                       # keine Patientendaten ins Log
            pass

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                page = (resources.files("stellwerk") / "web" / "index.html").read_bytes()
                self._send(200, page, "text/html; charset=utf-8")
            elif path.startswith("/api/"):
                self._api("GET")
            elif path == "/probieren":
                opts = "".join(f'<option value="{t}">{t}</option>' for t in list_templates())
                self._send(200, _PAGE.replace("__VORLAGEN__", opts).encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/v1/health":
                llm, note = make_llm(cfg)
                self._json(200, {"status": "ok", "version": __version__, "modell": llm.label if llm else note})
            else:
                self._json(404, {"fehler": "Unbekannter Pfad. Vorhanden: GET /, GET /v1/health, POST /v1/convert"})

        def _api(self, method: str):
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BYTES:
                return self._json(413, {"fehler": f"Datei größer als {MAX_BYTES // 1024 // 1024} MB."})
            body = self.rfile.read(n) if n else b""
            try:
                code, out = workbench.handle(ws, cfg, method, u.path, {k: v[0] for k, v in parse_qs(u.query).items()}, body)
            except workbench.ApiError as exc:
                return self._json(exc.code, {"fehler": str(exc)})
            except Exception as exc:
                return self._json(500, {"fehler": f"Verarbeitung abgebrochen: {exc}"})
            if isinstance(out, bytes):
                return self._send(code, out, "application/fhir+json; charset=utf-8")
            self._json(code, out)

        def do_PUT(self):
            self._api("PUT")

        def do_DELETE(self):
            self._api("DELETE")

        def do_POST(self):
            u = urlparse(self.path)
            if u.path.startswith("/api/"):
                return self._api("POST")
            if u.path != "/v1/convert":
                return self._json(404, {"fehler": "Unbekannter Pfad. Vorhanden: POST /v1/convert"})
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return self._json(400, {"fehler": "Leere Anfrage. Die Datei gehört unverändert in den Body."})
            if n > MAX_BYTES:
                return self._json(413, {"fehler": f"Datei größer als {MAX_BYTES // 1024 // 1024} MB."})
            data = self.rfile.read(n)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            meta = {"patient_id": q.get("patient_id"), "encounter_id": q.get("fall"), "doc_id": q.get("dokument_id"),
                    "author": q.get("autor"), "title": q.get("titel")}
            try:
                llm, note = make_llm(cfg)
                results = []
                for doc in ingest(data, q.get("name", "upload"), cfg, meta):
                    res = process(doc, cfg, q.get("vorlage") or None, llm, note)
                    deliver(res, cfg)
                    write_outputs(res, outdir)
                    results.append(res)
            except FileNotFoundError as exc:
                return self._json(400, {"fehler": str(exc)})
            except Exception as exc:
                return self._json(500, {"fehler": f"Verarbeitung abgebrochen: {exc}"})
            if "text/html" in (self.headers.get("Accept") or ""):
                return self._send(200, "<hr>".join(render_html(r) for r in results).encode("utf-8"), "text/html; charset=utf-8")
            out = [{**to_json(r), "bundle": r.bundle} for r in results]
            worst = "fehler" if any(r.status == "fehler" for r in results) else "pruefen" if any(r.status == "pruefen" for r in results) else "ok"
            self._json(200 if worst != "fehler" else 422, {"status": worst, "dokumente": out})

    return Handler


def serve(host: str, port: int, cfg: dict, outdir: str) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(cfg, outdir))
