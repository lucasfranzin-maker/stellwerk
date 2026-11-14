"""MLLP-Empfänger: Der Kommunikationsserver schickt HL7 v2, Stellwerk antwortet mit ACK.

Die Bestätigung AA heißt: Nachricht sicher auf Platte, mehr nicht. Inhaltliche Rückfragen
stehen im Prüfbericht und halten den sendenden Thread nicht an. AE gibt es nur, wenn die
Nachricht nicht gespeichert werden konnte (der Sender soll dann wiederholen).
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

from . import hl7v2
from .pipeline import deliver, ingest, make_llm, process, write_outputs
from .report import STATUS

SB, EB, CR = b"\x0b", b"\x1c", b"\x0d"


def handle_message(raw: bytes, cfg: dict, outdir: str, vorlage: str | None = None) -> tuple[bytes, list]:
    """Speichert, verarbeitet und liefert (ACK, Ergebnisse)."""
    if not raw.lstrip().startswith(b"MSH"):
        return hl7v2.ack(raw, "AR", "Keine HL7-Nachricht"), []
    try:
        spool = Path(outdir) / "eingang"
        spool.mkdir(parents=True, exist_ok=True)
        name = datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".hl7"
        (spool / name).write_bytes(raw)
    except OSError as exc:
        return hl7v2.ack(raw, "AE", f"Nicht gespeichert: {exc}"), []
    results = []
    try:
        llm, note = make_llm(cfg)
        for doc in ingest(raw, name, cfg):
            res = process(doc, cfg, vorlage, llm, note)
            deliver(res, cfg)
            write_outputs(res, outdir)
            results.append(res)
    except Exception as exc:                                    # Verarbeitung darf den Empfang nie stören
        print(f"FEHLER   {name}: Verarbeitung abgebrochen ({exc}). Die Nachricht liegt in {spool / name}.", file=sys.stderr)
    return hl7v2.ack(raw, "AA"), results


async def _client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, cfg, outdir, vorlage):
    buf = b""
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            buf += chunk
            while EB in buf:
                frame, buf = buf.split(EB, 1)
                buf = buf.lstrip(CR)
                raw = frame.split(SB, 1)[-1]
                ackmsg, results = await asyncio.to_thread(handle_message, raw, cfg, outdir, vorlage)
                writer.write(SB + ackmsg + EB + CR)
                await writer.drain()
                for r in results:
                    print(f"{STATUS[r.status]:<8} {r.doc.meta.doc_id or r.doc.name} · {r.delivery} · {r.files.get('bericht', '')}", flush=True)
    finally:
        writer.close()


async def serve(host: str, port: int, cfg: dict, outdir: str, vorlage: str | None = None, ready=None):
    server = await asyncio.start_server(lambda r, w: _client(r, w, cfg, outdir, vorlage), host, port)
    if ready is not None:
        ready(server.sockets[0].getsockname()[1])
    async with server:
        await server.serve_forever()
