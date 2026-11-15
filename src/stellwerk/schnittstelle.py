"""BridgeLink-Kanal je Schnittstelle.

Aus einer Aufgabe mit Feld-Mapping (Datenbank -> Patient, Observation) entsteht ein fertiger Kanal:

  Database Reader   liest die Abfrage der Aufgabe, nur neue Zeilen (Fortschrittsmarke), Obergrenze je Lauf
       |            Zeilen ohne Patientennummer oder ohne Messwert werden abgewiesen, mit Grund in der Nachrichtenliste
  Filter + Skript   baut das Bundle nach den Regeln des Übergabepakets (kanal_bauen.js, dieselbe Datei wie in den Tests)
       |
       +-- ok ------------> FHIR-Ziel (Warteschlange, Wiederholung jede Minute)
       +-- Prüfpunkte ----> Prüfablage (Bundle) und Prüfhinweise (Textdatei, ohne Patientendaten)

Der Kanal kennt Stellwerk zur Laufzeit nicht. Stellwerk ist das Entwurfswerkzeug, BridgeLink die Laufzeit.
Dokument-Schnittstellen (PDF, HL7, Textspalten) braucht die Lese- und Zuordnungslogik von Stellwerk; dafür siehe
`dokument_kanal`.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from importlib import resources
from xml.sax.saxutils import escape

from sqlalchemy.engine import make_url

from . import bridgelink as bl
from . import uebergabe
from .sqlsource import SqlError, resolve_url
from .workbench import KANAL_VORGABE, ApiError, Workspace

HINWEIS_CODES = ("TEXTWERT", "GESCHLECHT", "DATUM")

_JDBC = {
    "sqlite": ("org.sqlite.JDBC", lambda u: f"jdbc:sqlite:{u.database}"),
    "postgresql": ("org.postgresql.Driver", lambda u: f"jdbc:postgresql://{u.host}:{u.port or 5432}/{u.database}"),
    "mssql": ("com.microsoft.sqlserver.jdbc.SQLServerDriver",
              lambda u: f"jdbc:sqlserver://{u.host}:{u.port or 1433};databaseName={u.database}"),
    "oracle": ("oracle.jdbc.OracleDriver",
               lambda u: f"jdbc:oracle:thin:@//{u.host}:{u.port or 1521}/{(u.query.get('service_name') or u.database or '')}"),
    "mysql": ("com.mysql.cj.jdbc.Driver", lambda u: f"jdbc:mysql://{u.host}:{u.port or 3306}/{u.database}"),
    "mariadb": ("com.mysql.cj.jdbc.Driver", lambda u: f"jdbc:mysql://{u.host}:{u.port or 3306}/{u.database}"),
}


@dataclass
class Kanal:
    id: str
    name: str
    xml: str
    hinweise: list[str] = field(default_factory=list)       # was der Administrator wissen muss
    paket: dict[str, bytes] = field(default_factory=dict)   # Dateien des Übergabepakets (Zuordnung, Testfälle)


def jdbc(db: str, ueberschreiben: str = "") -> dict:
    """SQLAlchemy-Adresse der Aufgabe in Treiber, JDBC-Adresse, Benutzer und Passwort für BridgeLink übersetzen."""
    try:
        u = make_url(resolve_url(db))
    except SqlError:
        raise
    except Exception as exc:                                    # noqa: BLE001 - make_url wirft verschiedene Fehler
        raise ApiError(400, f"Die Datenbankadresse lässt sich nicht lesen: {exc}") from exc
    art = u.get_backend_name()
    if art not in _JDBC:
        raise ApiError(400, f"Für „{art}“ kennt Stellwerk keine JDBC-Adresse. Unterstützt: {', '.join(sorted(_JDBC))}.")
    treiber, bauen = _JDBC[art]
    if art == "sqlite" and not str(u.database or "").startswith("/"):
        raise ApiError(400, "BridgeLink braucht für SQLite einen absoluten Pfad (sqlite:////pfad/zur/datei.db).")
    return {"art": art, "treiber": treiber, "url": ueberschreiben or bauen(u), "benutzer": u.username or "", "passwort": u.password or ""}


# --------------------------------------------------------------------------
# Skripte
# --------------------------------------------------------------------------

def _js(obj) -> str:
    """JSON als JavaScript-Ausdruck (ES5: U+2028/2029 maskieren)."""
    return json.dumps(obj, ensure_ascii=False).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


_KOPF = """\
// Von Stellwerk erzeugt. Änderungen gehen beim erneuten Einrichten verloren.
var K = %s;
function bez(n) { return /^[A-Za-z_][A-Za-z0-9_]*$/.test(n) ? n : '"' + n.replace(/"/g, '""') + '"'; }
function wertVon(t) { return K.art === 'zahl' ? new java.math.BigDecimal(t) : java.sql.Timestamp.valueOf(t); }
function stand() {
  var f = new java.io.File(K.standDatei);
  if (f.exists()) {
    var t = String(new java.lang.String(java.nio.file.Files.readAllBytes(f.toPath()), 'UTF-8')).trim();
    if (t) { return t; }
  }
  return K.start ? String(K.start) : null;
}
"""

_AUSWAHL = """\
function text(v) {
  if (v instanceof java.sql.Timestamp || v instanceof java.sql.Date || v instanceof java.sql.Time) { return String(v.toString()); }
  if (v instanceof java.math.BigDecimal) { return String(v.toPlainString()); }
  if (v instanceof java.sql.Clob) { return String(v.getSubString(1, v.length())); }
  return String(v);
}
var sql = K.abfrage, params = [];
if (K.spalte) {
  var s = stand();
  sql = 'SELECT * FROM (\\n' + K.abfrage + '\\n) stellwerk_q';
  if (s !== null) { sql += ' WHERE ' + bez(K.spalte) + ' > ?'; params.push(wertVon(s)); }
  sql += ' ORDER BY ' + bez(K.spalte);
}
var db = DatabaseConnectionFactory.createDatabaseConnection(K.treiber, K.url, K.benutzer, K.passwort);
try {
  var ps = db.getConnection().prepareStatement(sql);
  ps.setMaxRows(K.max);
  for (var i = 0; i < params.length; i++) { ps.setObject(i + 1, params[i]); }
  var rs = ps.executeQuery(), md = rs.getMetaData(), n = md.getColumnCount(), zeilen = new java.util.ArrayList();
  while (rs.next()) {
    var m = new java.util.LinkedHashMap();
    for (var c = 1; c <= n; c++) {
      var v = rs.getObject(c);
      if (v !== null) { m.put(String(md.getColumnLabel(c)).toLowerCase(), text(v)); }
    }
    zeilen.add(m);
  }
  rs.close(); ps.close();
  return zeilen;
} finally { db.close(); }
"""

_FORTSCHRITT = """\
// Fortschrittsmarke: höchster Wert der Spalte, der als Nachricht angenommen wurde (Datei im BridgeLink-Server).
var w = resultMap.get(K.spalte.toLowerCase());
if (w !== null && w !== undefined) {
  var neu = String(w), alt = stand();
  if (alt === null || wertVon(neu).compareTo(wertVon(alt)) > 0) {
    var f = new java.io.File(K.standDatei), tmp = new java.io.File(K.standDatei + '.tmp');
    f.getParentFile().mkdirs();
    var out = new java.io.FileOutputStream(tmp);
    out.write(new java.lang.String(neu).getBytes('UTF-8'));
    out.close();
    tmp.renameTo(f);
  }
}
"""

_UMGEBUNG = """\
var env = {
  offset: function (zone, y, mo, d, h, mi, s) {     // Versatz vor der Umstellung, wie Python zoneinfo
    var ldt = java.time.LocalDateTime.of(y, mo, d, h, mi, s), regeln = java.time.ZoneId.of(zone).getRules();
    var tr = regeln.getTransition(ldt);
    var id = String((tr !== null ? tr.getOffsetBefore() : regeln.getOffset(ldt)).getId());
    return id === 'Z' ? '+00:00' : id;
  },
  uuid: function () { return String(java.util.UUID.randomUUID()); }
};
"""

_BAUEN = """\
var r = StellwerkBauen.bauen(StellwerkBauen.zeileLesen(String(msg)), Z, env), kritisch = [], codes = [], zeilen = [];
for (var i = 0; i < r.hinweise.length; i++) {
  var h = r.hinweise[i];
  if (codes.indexOf(h.code) < 0) { codes.push(h.code); }
  zeilen.push(h.code + ': ' + h.text);
  if (OPT.zustellen.indexOf(h.code) < 0) { kritisch.push(h); }
}
if (r.bundle === null) {
  channelMap.put('stellwerkStatus', 'ohne');
  channelMap.put('stellwerkGrund', r.grund);
  return false;
}
var id = String(connectorMessage.getMessageId());
channelMap.put('stellwerkStatus', kritisch.length ? 'pruefen' : 'ok');
channelMap.put('stellwerkGrund', '');
channelMap.put('stellwerkHinweise', codes.join(','));
channelMap.put('stellwerkHinweisText', zeilen.join('\\n'));
channelMap.put('stellwerkBundle', StellwerkBauen.json(r.bundle));
channelMap.put('stellwerkDateiname', id + '.json');
channelMap.put('stellwerkHinweisDatei', id + '.hinweise.txt');
return true;
"""


def _bauer() -> str:
    return (resources.files("stellwerk") / "kanal_bauen.js").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Prüfung der Einstellungen
# --------------------------------------------------------------------------

def kanal_einstellungen(t: dict) -> dict:
    return {**KANAL_VORGABE, **(t.get("kanal") or {})}


def _pruefe_zeitzonen(cols: list[str], rows: list[tuple], regeln: dict) -> list[str]:
    """Zeitangaben mit Zeitzone (TIMESTAMPTZ) liest JDBC in der Zeitzone des BridgeLink-Servers, Stellwerk in der der Sitzung."""
    low = {c: i for i, c in enumerate(cols)}
    return [f"Die Spalte „{c}“ enthält Zeitangaben mit Zeitzone (zum Beispiel TIMESTAMPTZ). Der Kanal liest sie in der Zeitzone des "
            "BridgeLink-Servers und könnte einen anderen Zeitpunkt schreiben als Stellwerk. Bitte in der Abfrage als lokale Zeit ohne Zone "
            "ausgeben (zum Beispiel abnahme_zeit AT TIME ZONE 'Europe/Berlin')."
            for c, r in regeln.items() if r["ziel"] in ("obs.zeit", "patient.birthDate")
            and any(getattr(row[low[c]], "tzinfo", None) is not None for row in rows)]


def _pruefe_neu(ke: dict, cols: list[str], rows: list[tuple], regeln: dict, art: str = "") -> list[str]:
    """Woran erkennt der Kanal neue Zeilen? Liefert Fehlermeldungen (leer = in Ordnung)."""
    sp = (ke["neu_spalte"] or "").strip()
    if not sp and not ke["vollabfrage"]:
        return ["Für den Betrieb festlegen, woran neue Zeilen erkannt werden: eine Spalte mit laufender Nummer oder Zeitstempel "
                "(Spalte unter „Neue Zeilen“ wählen), oder ausdrücklich „alle Zeilen bei jedem Lauf lesen“."]
    if not sp:
        if not any(r["ziel"] == "auftrag.id" for r in regeln.values()):
            return ["Ohne Auftragsnummer würde jeder Lauf dieselben Ergebnisse im CDR neu anlegen. Entweder eine Spalte für neue Zeilen "
                    "wählen oder eine Spalte der Auftragsnummer zuordnen."]
        return []
    low = {c.lower(): i for i, c in enumerate(cols)}
    if sp.lower() not in low:
        return [f"Die Spalte „{sp}“ für neue Zeilen steht nicht im Ergebnis der Abfrage."]
    werte = [r[low[sp.lower()]] for r in rows if r[low[sp.lower()]] not in (None, "")]
    if ke["neu_art"] == "zahl":
        if any(not re.match(r"^-?\d+(?:\.\d+)?$", str(w).strip()) for w in werte):
            return [f"Die Spalte „{sp}“ enthält Werte, die keine Zahl sind. Für Zahlen als Kriterium muss sie eine laufende Nummer sein."]
    elif ke["neu_art"] == "zeit":
        if art == "sqlite":
            return ["SQLite speichert Zeitangaben als Text; sie lassen sich nicht zuverlässig vergleichen. Eine laufende Nummer wählen."]
        if any(not (isinstance(w, (datetime, date)) or re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?$", str(w).strip()))
               for w in werte):
            return [f"Die Spalte „{sp}“ ist keine Datum-Zeit-Spalte. Als Text gespeicherte Zeitangaben lassen sich nicht zuverlässig "
                    "vergleichen; dann eine laufende Nummer wählen."]
    else:
        return ["Die Art des Kriteriums für neue Zeilen muss „zahl“ oder „zeit“ sein."]
    return []


# --------------------------------------------------------------------------
# Kanal für Feld-Mapping
# --------------------------------------------------------------------------

def felder_kanal(ws: Workspace, tid: str, cfg: dict, version: str = "26.9.0") -> Kanal:
    """Kanal für eine Feld-Mapping-Aufgabe bauen. Wirft ApiError (400/409) mit einer Meldung, die sagt, was zu tun ist."""
    try:
        return _felder_kanal(ws, tid, cfg, version)
    except bl.BridgeLinkError as exc:                          # fehlende Zugangsdaten oder ungültige Kanaleinstellungen
        raise ApiError(409, str(exc)) from exc


def _felder_kanal(ws: Workspace, tid: str, cfg: dict, version: str) -> Kanal:
    t = ws.task(tid)
    if not (t["art"] == "sql" and t["modus"] == "felder"):
        raise ApiError(400, "Diese Aufgabe ist kein Feld-Mapping aus einer Datenbank. Für Dokumente: Dokument-Schnittstelle.")
    ke = kanal_einstellungen(t)
    name = re.sub(r"\s+", " ", uebergabe.task_name(ws, tid)).strip()
    cols, rows = ws._query(t)
    fd = t["felder"]
    regeln = {c: r for c, r in fd["regeln"].items() if c in cols and r.get("ziel")}
    verbindung = jdbc(t["db"], str(ke.get("jdbc_url") or ""))
    fehler = _pruefe_neu(ke, cols, rows, regeln, verbindung["art"]) + _pruefe_zeitzonen(cols, rows, regeln)
    if fehler:
        raise ApiError(409, fehler[0])
    paket = uebergabe._felder(ws, t, cfg, name, False)           # wirft 409, wenn die Zuordnung Fehler hat
    Z = json.loads(paket["zuordnung.json"])
    z_klein = {"ziel": Z["ziel"], "geschlecht_codes": Z["geschlecht_codes"],
               "spalten": [{k: s[k] for k in ("spalte", "stand", "ziel", "loinc", "anzeige", "einheit_spalte", "einheit_fest")}
                           for s in Z["spalten"]]}
    k = (cfg.get("bridgelink") or {}).get("kanal") or {}
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "schnittstelle"
    cid = str(uuid.uuid5(bl.KANAL_NS, "schnittstelle:" + tid))
    ablage = (k.get("pruefablage") or "/opt/bridgelink/appdata/stellwerk/pruefen").rstrip("/") + "/" + slug
    stand_ordner = (k.get("stand_ordner") or "/opt/bridgelink/appdata/stellwerk/stand").rstrip("/")
    ziel = (k.get("fhir_ziel") or "").strip()

    abfrage = t["abfrage"].strip().rstrip(";")
    if ke["neu_spalte"]:
        if "?" in abfrage:
            raise ApiError(409, "Die Abfrage enthält ein Fragezeichen. BridgeLink würde es als Platzhalter lesen; bitte aus der Abfrage entfernen "
                                "(zum Beispiel statt eines Textes mit ? die Bedingung umschreiben).")
        if verbindung["art"] == "mssql" and re.search(r"\border\s+by\b", abfrage, re.I):
            raise ApiError(409, "SQL Server erlaubt kein ORDER BY in einer Unterabfrage. ORDER BY aus der Abfrage nehmen; der Kanal sortiert selbst.")
    K = {**{k: v for k, v in verbindung.items() if k != "art"}, "abfrage": abfrage, "spalte": (ke["neu_spalte"] or "").strip(), "art": ke["neu_art"],
         "start": str(ke["start"] or ""), "max": int(ke["max_zeilen"]), "standDatei": f"{stand_ordner}/{slug}.stand"}
    opt = {"zustellen": [c for c in ke["zustellen"] if c in HINWEIS_CODES]}
    kopf = _KOPF % _js(K)
    select = kopf + _AUSWAHL
    update = kopf + _FORTSCHRITT
    filter_js = f"var Z = {_js(z_klein)};\nvar OPT = {_js(opt)};\n" + _bauer() + "\n" + _UMGEBUNG + _BAUEN

    poll = (
        '<pollConnectorProperties version="{v}"><pollingType>INTERVAL</pollingType><pollOnStart>true</pollOnStart>'
        f'<pollingFrequency>{max(5, int(ke["intervall"])) * 1000}</pollingFrequency><pollingHour>0</pollingHour><pollingMinute>0</pollingMinute>'
        '<cronJobs/><pollConnectorPropertiesAdvanced><weekly>true</weekly><inactiveDays><boolean>false</boolean><boolean>false</boolean>'
        '<boolean>false</boolean><boolean>false</boolean><boolean>false</boolean><boolean>false</boolean><boolean>false</boolean>'
        '<boolean>false</boolean></inactiveDays><dayOfMonth>1</dayOfMonth><allDay>true</allDay><startingHour>8</startingHour>'
        '<startingMinute>0</startingMinute><endingHour>17</endingHour><endingMinute>0</endingMinute></pollConnectorPropertiesAdvanced>'
        '</pollConnectorProperties>')
    source = (
        '<sourceConnector version="{v}"><metaDataId>0</metaDataId><name>sourceConnector</name>'
        '<properties class="com.mirth.connect.connectors.jdbc.DatabaseReceiverProperties" version="{v}"><pluginProperties/>'
        + poll +
        '<sourceConnectorProperties version="{v}"><responseVariable>None</responseVariable><respondAfterProcessing>true</respondAfterProcessing>'
        '<processBatch>false</processBatch><firstResponse>false</firstResponse><processingThreads>1</processingThreads>'
        f'{bl._resources()}<queueBufferSize>1000</queueBufferSize></sourceConnectorProperties>'
        f'<driver>{escape(verbindung["treiber"])}</driver><url>{escape(verbindung["url"])}</url>'
        f'<username>{escape(verbindung["benutzer"])}</username><password>{escape(verbindung["passwort"])}</password>'
        f'<select>{escape(select)}</select><update>{escape(update)}</update><useScript>true</useScript>'
        '<aggregateResults>false</aggregateResults><cacheResults>true</cacheResults><keepConnectionOpen>false</keepConnectionOpen>'
        f'<updateMode>{3 if K["spalte"] else 1}</updateMode><retryCount>3</retryCount><retryInterval>10000</retryInterval>'
        '<fetchSize>1000</fetchSize><encoding>DEFAULT_ENCODING</encoding></properties>'
        + bl._raw("transformer").replace(
            "<elements/>", '<elements><com.mirth.connect.plugins.javascriptstep.JavaScriptStep version="{v}"><name>Bundle übernehmen</name>'
                           '<sequenceNumber>0</sequenceNumber><enabled>true</enabled><script>'
                           + escape("msg = channelMap.get('stellwerkBundle');") + '</script></com.mirth.connect.plugins.javascriptstep.JavaScriptStep></elements>')
        + bl._filter("Bundle nach den Regeln der Übergabe bauen", filter_js)
        + '<transportName>Database Reader</transportName><mode>SOURCE</mode><enabled>true</enabled><waitForPrevious>true</waitForPrevious>'
          '</sourceConnector>')

    dests = []
    if ziel:
        dests.append(bl._connector(1, "FHIR-Ziel", bl.fhir_ziel_props(k), "HTTP Sender",
                                   bl._filter("Freigegebene Bundles", "return $('stellwerkStatus') == 'ok';"), bl.ablehnung_skript(k, ablage)))
    dests.append(bl._connector(len(dests) + 1, "Prüfablage" if ziel else "Ablage", bl.datei_props(ablage), "File Writer",
                               bl._filter("Bundles mit Prüfpunkten" if ziel else "Alle Bundles",
                                          "return $('stellwerkStatus') != 'ok';" if ziel else "return true;")))
    dests.append(bl._connector(len(dests) + 1, "Prüfhinweise", bl.datei_props(ablage, "${stellwerkHinweisDatei}", "${stellwerkHinweisText}"),
                               "File Writer", bl._filter("Nur Bundles mit Prüfpunkten", "return $('stellwerkStatus') == 'pruefen';")))
    meta = bl.meta_xml((("STATUS", "stellwerkStatus"), ("GRUND", "stellwerkGrund"), ("HINWEISE", "stellwerkHinweise")))
    beschreibung = (f"Schnittstelle „{name}“. Liest {verbindung['url']} nach der Abfrage aus Stellwerk, baut je Zeile ein FHIR-Bundle "
                    "nach den Regeln der Übergabe und stellt es dem FHIR-Ziel zu; Bundles mit Prüfpunkten landen in der Prüfablage. "
                    "Von Stellwerk erzeugt; Änderungen hier werden bei erneutem Einrichten überschrieben.")
    xml = bl.kanal_xml(cid, f"Stellwerk {name}", beschreibung, source, dests, meta, k, version)

    hinweise = []
    if not ziel:
        hinweise.append("bridgelink.kanal.fhir_ziel ist leer: Alle Bundles landen in der Ablage im BridgeLink-Server, keines geht an ein CDR.")
    if ziel and k.get("bei_ablehnung", "abweisen") == "warten":
        hinweise.append("bei_ablehnung ist „warten“: Lehnt das CDR ein Bundle dauerhaft ab (HTTP 400, 422), bleibt es in der Warteschlange "
                        "und hält alle folgenden auf, bis jemand in BridgeLink eingreift.")
    elif ziel:
        hinweise.append(f"Lehnt das CDR ein Bundle dauerhaft ab (HTTP {', '.join(map(str, bl.ABLEHNUNG_CODES))}), geht es als Fehler in das "
                        f"Dashboard von BridgeLink und mit Hinweis in {ablage}/abgelehnt; die folgenden Bundles werden nicht aufgehalten. "
                        "Bei Ausfall oder Anmeldefehlern (401, 403, 5xx) wartet es in der Warteschlange.")
    if K["spalte"]:
        hinweise.append(f"Neue Zeilen: Spalte „{K['spalte']}“, größer als der Stand in {K['standDatei']}. Ohne diese Datei liest der erste Lauf "
                        + (f"ab Wert {K['start']}." if K["start"] else "alle Zeilen (bis zu den Zeilen je Lauf, dann beim nächsten Lauf weiter)."))
    else:
        hinweise.append("Alle Zeilen werden bei jedem Lauf gelesen. Das CDR bekommt sie mit Auftragsnummer als Aktualisierung (kein Duplikat), "
                        "belastet aber Quelle und CDR.")
    if Z["status"] == "pruefen":
        hinweise.append("Die Zuordnung hat offene Punkte (siehe Übergabepaket). Sie gelten für jeden Lauf.")
    if verbindung["passwort"]:
        hinweise.append("Der Kanal enthält das Passwort der Datenbank. Nur im Administrator von BridgeLink ansehen, nicht weitergeben.")
    return Kanal(cid, f"Stellwerk {name}", xml, hinweise, paket)


def einrichten(kanal: Kanal, cfg: dict) -> str:
    """Kanal in BridgeLink anlegen oder ersetzen und bereitstellen. Liefert „angelegt“ oder „aktualisiert“.

    Ist der Data Pruner dort aus, wird eine Warnung an kanal.hinweise angehängt (sie läuft ins Leere, wenn man sie übersieht)."""
    if not (cfg.get("bridgelink") or {}).get("api"):
        raise ApiError(409, "BridgeLink ist nicht eingerichtet (bridgelink.api, bridgelink.benutzer und das Passwort aus bridgelink.passwort_env).")
    try:
        server = bl.BridgeLink(cfg.get("bridgelink") or {})
        was = server.install(kanal.id, kanal.xml)
        warnung = server.pruner_hinweis()
    except bl.BridgeLinkError as exc:
        raise ApiError(502, str(exc)) from exc
    if warnung:
        kanal.hinweise.append(warnung)
    return was
