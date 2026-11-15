"""Datenhaltung: Projekte, Aufgaben, Läufe, Erschließungsentscheidungen und Audit-Protokoll.

SQLite für den Betrieb im Haus, PostgreSQL für Cloud oder größere Installationen.
Das Schema wird mit Alembic verwaltet (stellwerk/migrations); beim Start wird es auf den neuesten Stand gebracht.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, Text,
                        create_engine, delete, event, func, select)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Node(Base):
    """Eintrag im Projektbaum: Ordner oder Aufgabe."""
    __tablename__ = "knoten"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    parent_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("knoten.id", ondelete="CASCADE"), index=True)
    typ: Mapped[str] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(120))
    position: Mapped[int] = mapped_column(Integer, default=0)
    offen: Mapped[bool] = mapped_column(Boolean, default=True)


class Task(Base):
    """Einstellungen, Quelle und letztes Bundle einer Aufgabe."""
    __tablename__ = "aufgabe"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    einstellungen: Mapped[dict] = mapped_column(JSON, default=dict)
    quelle_name: Mapped[str] = mapped_column(String(120), default="")
    quelle: Mapped[bytes | None] = mapped_column(LargeBinary)
    bundle: Mapped[bytes | None] = mapped_column(LargeBinary)
    geaendert: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Run(Base):
    """Ein Verarbeitungslauf. Grundlage für Monitoring. Enthält keine Patientendaten."""
    __tablename__ = "lauf"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    zeit: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    kanal: Mapped[str] = mapped_column(String(20))            # workbench | convert | mllp | cli
    aufgabe_id: Mapped[str | None] = mapped_column(String(32), index=True)
    quelle: Mapped[str] = mapped_column(String(120), default="")   # Konnektor oder Aufgabenname, nie ein Patientenname
    art: Mapped[str] = mapped_column(String(10), default="")
    status: Mapped[str] = mapped_column(String(10))           # ok | pruefen | fehler
    fehler: Mapped[int] = mapped_column(Integer, default=0)
    pruefen: Mapped[int] = mapped_column(Integer, default=0)
    dauer_ms: Mapped[int] = mapped_column(Integer, default=0)
    codes: Mapped[list] = mapped_column(JSON, default=list)  # Befundcodes, ohne Meldungstext


class Decision(Base):
    """Von Hand festgelegte Bedeutungen bei der Datenbankerschließung."""
    __tablename__ = "erschliessung"
    db: Mapped[str] = mapped_column(String(500), primary_key=True)
    entscheidungen: Mapped[dict] = mapped_column(JSON, default=dict)


class Audit(Base):
    """Wer hat wann was geändert oder ausgelöst. Ohne Inhalte, nur Kennungen."""
    __tablename__ = "audit"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    zeit: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    wer: Mapped[str] = mapped_column(String(200))
    aktion: Mapped[str] = mapped_column(String(60))
    ziel: Mapped[str] = mapped_column(String(200), default="")
    detail: Mapped[str] = mapped_column(Text, default="")


class Sitzung(Base):
    """Sitzung eines angemeldeten Nutzers. Der Cookie-Wert steht hier nie, nur sein SHA-256."""
    __tablename__ = "sitzung"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    art: Mapped[str] = mapped_column(String(10))                       # nutzer
    nutzer: Mapped[str] = mapped_column(String(200), default="")       # Kennung beim Identitätsdienst (sub)
    name: Mapped[str] = mapped_column(String(200), default="")
    rollen: Mapped[list] = mapped_column(JSON, default=list)
    daten: Mapped[dict] = mapped_column(JSON, default=dict)            # ID-Token als Hinweis beim Abmelden
    erstellt: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"))      # Sekunden seit 1970 (UTC)
    zuletzt: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"))
    laeuft_ab: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), index=True)


class Store:
    def __init__(self, url: str):
        if url.startswith("sqlite:///"):
            Path(url[10:]).parent.mkdir(parents=True, exist_ok=True)
        self.url = url
        try:
            self.engine = create_engine(url, future=True, pool_pre_ping=True)
        except ImportError as exc:
            from .sqlsource import SqlError, driver_missing
            raise SqlError("Datenbank von Stellwerk: " + driver_missing(exc)) from exc
        if self.engine.dialect.name == "sqlite":
            @event.listens_for(self.engine, "connect")
            def _fk(dbapi_con, _):                                  # Fremdschlüssel und gleichzeitiges Lesen
                cur = dbapi_con.cursor()
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA journal_mode=WAL")
                cur.close()
        self.session = sessionmaker(self.engine, expire_on_commit=False)

    def migrate(self) -> None:
        from alembic import command
        from alembic.config import Config
        cfg = Config()
        cfg.set_main_option("script_location", str(resources.files("stellwerk") / "migrations"))
        cfg.attributes["engine"] = self.engine
        command.upgrade(cfg, "head")

    @staticmethod
    def migration_sql(dialect: str) -> str:
        """SQL der Migrationen für einen Datenbanktyp, ohne Verbindung (postgresql, sqlite)."""
        import io

        from alembic import command
        from alembic.config import Config
        buf = io.StringIO()
        cfg = Config(output_buffer=buf)
        cfg.set_main_option("script_location", str(resources.files("stellwerk") / "migrations"))
        cfg.attributes.update(dialekt=dialect, ausgabe=buf)
        command.upgrade(cfg, "head", sql=True)
        return buf.getvalue()

    def ping(self) -> bool:
        with self.engine.connect() as con:
            con.exec_driver_sql("SELECT 1")
        return True

    # --- Baum
    def tree(self) -> list[dict]:
        with self.session() as s:
            nodes = s.scalars(select(Node).order_by(Node.position)).all()
        kids: dict[str | None, list[Node]] = {}
        for n in nodes:
            kids.setdefault(n.parent_id, []).append(n)

        def build(pid):
            out = []
            for n in kids.get(pid, []):
                d = {"id": n.id, "name": n.name, "typ": n.typ}
                if n.typ == "ordner":
                    d["offen"] = n.offen
                    d["kinder"] = build(n.id)
                out.append(d)
            return out
        return build(None)

    def replace_tree(self, nodes: list[dict]) -> set[str]:
        """Baum vollständig ersetzen. Liefert die Kennungen gelöschter Aufgaben."""
        flat: list[Node] = []

        def walk(lst, pid):
            for i, n in enumerate(lst):
                flat.append(Node(id=n["id"], parent_id=pid, typ=n["typ"], name=n["name"], position=i, offen=n.get("offen", True)))
                if n["typ"] == "ordner":
                    walk(n["kinder"], n["id"])
        walk(nodes, None)
        with self.session.begin() as s:
            alt = set(s.scalars(select(Node.id).where(Node.typ == "aufgabe")).all())
            s.query(Node).delete()
            s.flush()
            for n in flat:
                s.add(n)
            neu = {n.id for n in flat if n.typ == "aufgabe"}
            weg = alt - neu
            if weg:
                s.query(Task).filter(Task.id.in_(weg)).delete(synchronize_session=False)
        return weg

    # --- Aufgabe
    def task(self, tid: str) -> Task | None:
        with self.session() as s:
            return s.get(Task, tid)

    def save_task(self, tid: str, **fields) -> None:
        with self.session.begin() as s:
            t = s.get(Task, tid) or Task(id=tid, einstellungen={})
            for k, v in fields.items():
                setattr(t, k, v)
            s.add(t)

    # --- Läufe
    def add_run(self, **fields) -> None:
        with self.session.begin() as s:
            s.add(Run(**fields))

    def runs(self, limit: int = 200, aufgabe_id: str | None = None) -> list[dict]:
        with self.session() as s:
            q = select(Run).order_by(Run.id.desc()).limit(limit)
            if aufgabe_id:
                q = q.where(Run.aufgabe_id == aufgabe_id)
            return [{"id": r.id, "zeit": r.zeit.isoformat(), "kanal": r.kanal, "aufgabe_id": r.aufgabe_id, "quelle": r.quelle,
                     "art": r.art, "status": r.status, "fehler": r.fehler, "pruefen": r.pruefen, "dauer_ms": r.dauer_ms,
                     "codes": r.codes} for r in s.scalars(q)]

    # --- Erschließung
    def decisions(self, db: str) -> dict:
        with self.session() as s:
            d = s.get(Decision, db)
            return dict(d.entscheidungen) if d else {}

    def save_decisions(self, db: str, ent: dict) -> None:
        with self.session.begin() as s:
            d = s.get(Decision, db) or Decision(db=db)
            d.entscheidungen = ent
            s.add(d)

    # --- Audit
    def audit(self, wer: str, aktion: str, ziel: str = "", detail: str = "") -> None:
        with self.session.begin() as s:
            s.add(Audit(wer=wer[:200], aktion=aktion[:60], ziel=ziel[:200], detail=detail[:2000]))

    def audit_log(self, limit: int = 200) -> list[dict]:
        with self.session() as s:
            return [{"zeit": a.zeit.isoformat(), "wer": a.wer, "aktion": a.aktion, "ziel": a.ziel, "detail": a.detail}
                    for a in s.scalars(select(Audit).order_by(Audit.id.desc()).limit(limit))]

    # --- Sitzungen
    def sitzung(self, sid: str) -> dict | None:
        with self.session() as s:
            r = s.get(Sitzung, sid)
            return None if r is None else {"id": r.id, "art": r.art, "nutzer": r.nutzer, "name": r.name, "rollen": list(r.rollen or []),
                                           "daten": dict(r.daten or {}), "zuletzt": r.zuletzt, "laeuft_ab": r.laeuft_ab}

    def sitzung_anlegen(self, sid: str, art: str, laeuft_ab: int, jetzt: int, nutzer: str = "", name: str = "",
                        rollen: list | None = None, daten: dict | None = None) -> None:
        with self.session.begin() as s:
            s.add(Sitzung(id=sid, art=art, nutzer=nutzer, name=name, rollen=rollen or [], daten=daten or {},
                          erstellt=jetzt, zuletzt=jetzt, laeuft_ab=laeuft_ab))

    def sitzung_beruehren(self, sid: str, jetzt: int) -> None:
        with self.session.begin() as s:
            r = s.get(Sitzung, sid)
            if r:
                r.zuletzt = jetzt

    def sitzung_loeschen(self, sid: str) -> None:
        with self.session.begin() as s:
            r = s.get(Sitzung, sid)
            if r:
                s.delete(r)

    def sitzungen(self, jetzt: int) -> list[dict]:
        """Laufende Sitzungen angemeldeter Nutzer, ohne Kennungen, die sich als Cookie verwenden ließen."""
        with self.session() as s:
            return [{"name": r.name, "rolle": (r.rollen or [""])[-1], "seit": r.erstellt, "zuletzt": r.zuletzt, "bis": r.laeuft_ab}
                    for r in s.scalars(select(Sitzung).where(Sitzung.art == "nutzer", Sitzung.laeuft_ab >= jetzt).order_by(Sitzung.erstellt))]

    def sitzungen_beenden(self, name: str | None = None) -> int:
        """Sitzungen eines Nutzers beenden, ohne Namen alle. Liefert die Anzahl."""
        with self.session.begin() as s:
            q = delete(Sitzung).where(Sitzung.art == "nutzer")
            return s.execute(q.where(Sitzung.name == name) if name else q).rowcount or 0

    def sitzungen_aufraeumen(self, jetzt: int) -> None:
        with self.session.begin() as s:
            s.execute(delete(Sitzung).where(Sitzung.laeuft_ab < jetzt))

    def is_empty(self) -> bool:
        with self.session() as s:
            return not s.scalar(select(func.count()).select_from(Node))


def import_files(store: Store, root: str | Path) -> int:
    """Einmalige Übernahme eines Arbeitsbereichs aus Version 0.1 (baum.json und aufgaben/...). Liefert die Anzahl Aufgaben."""
    root = Path(root)
    baum = root / "baum.json"
    if not baum.exists() or not store.is_empty():
        return 0
    tree = json.loads(baum.read_text("utf-8")).get("kinder") or []
    store.replace_tree(tree)
    n = 0
    for d in (root / "aufgaben").glob("*") if (root / "aufgaben").exists() else []:
        fields: dict = {}
        if (d / "aufgabe.json").exists():
            fields["einstellungen"] = json.loads((d / "aufgabe.json").read_text("utf-8"))
        if (d / "quelle.bin").exists():
            fields["quelle"] = (d / "quelle.bin").read_bytes()
            fields["quelle_name"] = (d / "quelle.name").read_text("utf-8") if (d / "quelle.name").exists() else "quelle"
        if (d / "bundle.json").exists():
            fields["bundle"] = (d / "bundle.json").read_bytes()
        if fields:
            store.save_task(d.name, **fields)
            n += 1
    old = root / "erschliessung.json"
    if old.exists():
        for db, ent in json.loads(old.read_text("utf-8")).items():
            store.save_decisions(db, ent)
    return n
