"""HTTP-Dienst (FastAPI): Workbench, Schnittstelle für Kommunikationsserver, Betriebsendpunkte.

Pfade wie in Version 0.1, damit Oberfläche und Anbindungen unverändert weiterlaufen.
Ohne Anmeldung (kommt mit OIDC): nur an localhost binden oder hinter einen Proxy mit Anmeldung stellen.
"""
from __future__ import annotations

import logging
import re
import time
from importlib import resources

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from . import __version__, workbench
from .db import Store, import_files
from .fieldmap import dump_json
from .pipeline import deliver, ingest, make_llm, process, write_outputs
from .report import render_html, to_json
from .sections import list_templates

MAX_BYTES = 50 * 1024 * 1024
log = logging.getLogger("stellwerk")

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

# Die Workbench ist eine Seite mit eingebettetem Skript und Stil; bis zur React-Oberfläche braucht es 'unsafe-inline'.
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "font-src 'self'; connect-src 'self'; frame-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")


class JSONOut(JSONResponse):
    """JSON mit erhaltenen Nachkommastellen bei Messwerten (siehe fieldmap.dump_json)."""
    def render(self, content) -> bytes:
        return dump_json(content).encode("utf-8")


def create_app(cfg: dict, store: Store | None = None) -> FastAPI:
    store = store or Store(cfg["datenbank"])
    store.migrate()
    alt = cfg.get("arbeitsbereich")
    if alt:
        n = import_files(store, alt)
        if n:
            log.info("Arbeitsbereich aus Version 0.1 übernommen: %d Aufgaben", n)
    ws = workbench.Workspace(store)
    outdir = cfg.get("ausgabe") or "out"
    app = FastAPI(title="Stellwerk", version=__version__, docs_url=None, redoc_url=None, openapi_url="/api/openapi.json",
                  default_response_class=JSONOut)
    app.state.store = store

    def wer(request: Request) -> str:
        return "lokal"                                          # wird mit der Anmeldung durch den Nutzer ersetzt

    @app.middleware("http")
    async def guard(request: Request, call_next):
        n = request.headers.get("content-length")
        if n and n.isdigit() and int(n) > MAX_BYTES:
            return JSONOut({"fehler": f"Datei größer als {MAX_BYTES // 1024 // 1024} MB."}, status_code=413)
        resp = await call_next(request)
        h = resp.headers
        h.setdefault("Content-Security-Policy", CSP)
        h["X-Content-Type-Options"] = "nosniff"
        h["Referrer-Policy"] = "no-referrer"
        h["X-Frame-Options"] = "DENY"
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if not request.url.path.startswith("/fonts/"):
            h["Cache-Control"] = "no-store"
        return resp

    @app.exception_handler(workbench.ApiError)
    async def api_error(request: Request, exc: workbench.ApiError):
        return JSONOut({"fehler": str(exc)}, status_code=exc.code)

    @app.exception_handler(Exception)
    async def crash(request: Request, exc: Exception):
        log.exception("Unerwarteter Fehler bei %s %s", request.method, request.url.path)
        return JSONOut({"fehler": "Interner Fehler. Details stehen im Protokoll des Dienstes."}, status_code=500)

    async def body(request: Request) -> bytes:
        data = await request.body()
        if len(data) > MAX_BYTES:
            raise workbench.ApiError(413, f"Datei größer als {MAX_BYTES // 1024 // 1024} MB.")
        return data

    async def json_body(request: Request) -> dict:
        data = await body(request)
        try:
            import json
            val = json.loads(data.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise workbench.ApiError(400, "Kein gültiges JSON.") from exc
        if not isinstance(val, dict):
            raise workbench.ApiError(400, "JSON-Objekt erwartet.")
        return val

    # --- Seiten
    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse((resources.files("stellwerk") / "web" / "index.html").read_text("utf-8"))

    @app.get("/fonts/{name}")
    def font(name: str):
        if not re.fullmatch(r"[a-z0-9-]+\.woff2", name):
            raise workbench.ApiError(404, "Schrift nicht gefunden.")
        f = resources.files("stellwerk") / "web" / "fonts" / name
        if not f.is_file():
            raise workbench.ApiError(404, "Schrift nicht gefunden.")
        return Response(f.read_bytes(), media_type="font/woff2", headers={"Cache-Control": "max-age=86400"})

    @app.get("/probieren", response_class=HTMLResponse)
    def probieren():
        opts = "".join(f'<option value="{t}">{t}</option>' for t in list_templates())
        return HTMLResponse(_PAGE.replace("__VORLAGEN__", opts))

    # --- Betrieb
    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        try:
            store.ping()
        except Exception:
            return JSONOut({"status": "fehler", "datenbank": "nicht erreichbar"}, status_code=503)
        return {"status": "ok", "datenbank": store.engine.dialect.name}

    @app.get("/v1/health")
    def health():
        llm, note = make_llm(cfg)
        return {"status": "ok", "version": __version__, "modell": llm.label if llm else note, "datenbank": store.engine.dialect.name}

    # --- Workbench
    @app.get("/api/baum")
    def baum():
        return {**ws.tree(), "vorlagen": list_templates()}

    @app.put("/api/baum")
    async def baum_speichern(request: Request):
        weg = ws.save_tree(await json_body(request))
        store.audit(wer(request), "baum.gespeichert", detail=f"gelöschte Aufgaben: {', '.join(sorted(weg))}" if weg else "")
        return {**ws.tree(), "vorlagen": list_templates()}

    @app.get("/api/aufgabe/{tid}")
    def aufgabe(tid: str):
        return ws.task(tid)

    @app.put("/api/aufgabe/{tid}")
    async def aufgabe_speichern(tid: str, request: Request):
        out = ws.save_task(tid, await json_body(request))
        store.audit(wer(request), "aufgabe.gespeichert", tid)
        return out

    @app.post("/api/aufgabe/{tid}/quelle")
    async def quelle(tid: str, request: Request, name: str = "quelle"):
        ws.save_source(tid, name, await body(request))
        store.audit(wer(request), "quelle.hochgeladen", tid)
        return ws.task(tid)

    @app.post("/api/aufgabe/{tid}/lauf")
    def lauf(tid: str, request: Request):
        return ws.run(tid, cfg)

    @app.post("/api/aufgabe/{tid}/ablegen")
    async def ablegen(tid: str, request: Request):
        ordner = str((await json_body(request)).get("ordner", ""))
        out = ws.export(tid, ordner, cfg)
        store.audit(wer(request), "bundle.abgelegt", tid, ordner)
        return out

    @app.post("/api/aufgabe/{tid}/senden")
    def senden(tid: str, request: Request):
        out = ws.send(tid, cfg)
        store.audit(wer(request), "bundle.gesendet", tid, f"{out['bundles']} an {out['ziel']}")
        return out

    @app.get("/api/zustellung")
    def zustellung():
        z = cfg.get("zustellung") or {}
        ziel = str(z.get("ziel") or "")
        return {"ziel": ziel if ziel.startswith(("http://", "https://")) else "", "bei_pruefen": z.get("bei_pruefen", "zurueckhalten"),
                "bridgelink": bool((cfg.get("bridgelink") or {}).get("api"))}

    @app.get("/api/bridgelink/status")
    def bridgelink_status():
        from . import bridgelink
        b = cfg.get("bridgelink") or {}
        if not b.get("api"):
            raise workbench.ApiError(404, "BridgeLink ist nicht eingerichtet (bridgelink.api).")
        try:
            cid, _ = bridgelink.channel_xml(b.get("kanal") or {}, "x", "x")
            return bridgelink.BridgeLink(b).status(cid)
        except bridgelink.BridgeLinkError as exc:
            raise workbench.ApiError(502, str(exc)) from exc

    @app.get("/api/aufgabe/{tid}/bundle")
    def bundle(tid: str):
        data = ws.bundle(tid)
        if not data:
            raise workbench.ApiError(404, "Kein Bundle vorhanden.")
        return Response(data, media_type="application/fhir+json; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="bundle.json"'})

    @app.get("/api/aufgabe/{tid}/uebergabe")
    def uebergabe_paket(tid: str, request: Request, daten: int = 0):
        from . import uebergabe
        name, data = uebergabe.build_package(ws, tid, cfg, bool(daten))
        store.audit(wer(request), "uebergabe.erzeugt", tid, "mit echten Zeilen (Patientendaten)" if daten else "ohne Patientendaten")
        return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/api/erschliessen")
    async def erschliessen(request: Request):
        b = await json_body(request)
        db = str(b.get("db") or "")
        out = workbench.discover(ws, cfg, db, b.get("entscheidungen"))
        from .sqlsource import public_url
        store.audit(wer(request), "datenbank.erschlossen", public_url(db)[:200])
        return out

    @app.get("/api/laeufe")
    def laeufe(limit: int = 200, aufgabe: str | None = None):
        return {"laeufe": store.runs(min(max(limit, 1), 1000), aufgabe)}

    @app.get("/api/audit")
    def audit(limit: int = 200):
        return {"eintraege": store.audit_log(min(max(limit, 1), 1000))}

    # --- Schnittstelle für Kommunikationsserver
    @app.post("/v1/convert")
    async def convert(request: Request, name: str = "upload", vorlage: str | None = None, patient_id: str | None = None,
                      fall: str | None = None, dokument_id: str | None = None, autor: str | None = None, titel: str | None = None):
        data = await body(request)
        if not data:
            raise workbench.ApiError(400, "Leere Anfrage. Die Datei gehört unverändert in den Body.")
        meta = {"patient_id": patient_id, "encounter_id": fall, "doc_id": dokument_id, "author": autor, "title": titel}
        llm, note = make_llm(cfg)
        results = []
        for doc in ingest(data, name, cfg, meta):
            t0 = time.monotonic()
            try:
                res = process(doc, cfg, vorlage or None, llm, note)
            except FileNotFoundError as exc:
                raise workbench.ApiError(400, str(exc)) from exc
            deliver(res, cfg)
            write_outputs(res, outdir)
            record(store, "convert", res, t0)
            results.append(res)
        if "text/html" in (request.headers.get("accept") or ""):
            return HTMLResponse("<hr>".join(render_html(r) for r in results))
        out = [{**to_json(r), "bundle": r.bundle} for r in results]
        worst = "fehler" if any(r.status == "fehler" for r in results) else "pruefen" if any(r.status == "pruefen" for r in results) else "ok"
        return JSONOut({"status": worst, "dokumente": out}, status_code=200 if worst != "fehler" else 422)

    return app


def record(store: Store, kanal: str, res, t0: float) -> None:
    """Lauf ohne Patientendaten protokollieren: Kanal, Quelle (Systemname), Status, Befundcodes."""
    try:
        store.add_run(kanal=kanal, quelle=(res.doc.meta.source_system or res.doc.kind)[:120], art=res.doc.kind,
                      status=res.status, fehler=sum(1 for f in res.findings if f.severity == "fehler"),
                      pruefen=sum(1 for f in res.findings if f.severity == "pruefen"),
                      dauer_ms=int((time.monotonic() - t0) * 1000), codes=sorted({f.code for f in res.findings}))
    except Exception:
        log.exception("Lauf konnte nicht protokolliert werden")
