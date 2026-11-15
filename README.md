# Stellwerk

Stellwerk entwirft Schnittstellen ins FHIR-CDR. Es liest HL7-v2-Nachrichten, KIS-Datenbanken und PDFs (auch schlechte
Scans), schlägt vor, wie sich die Inhalte auf FHIR abbilden, und zeigt zu jedem Ergebnis, was zu prüfen ist und was man
dann tut. Die fertige Schnittstelle übernimmt der Kommunikationsserver (Cloverleaf, BridgeLink, Mirth), der sie im
Betrieb fährt.

Stand: Version 0.3, Prototyp, bisher nur mit erfundenen Daten. Was geprüft ist und was nicht, steht unter [Grenzen](#grenzen).

## Wie die Teile zusammenspielen

```
HL7 v2   ─┐
KIS-DB   ─┼─▶ Stellwerk: erschließen, zuordnen, prüfen
PDF/Scan ─┘        │
                   ├─ Übergabepaket (ZIP) ─▶ Cloverleaf, Mirth: der Administrator baut nach
                   └─ fertiger Kanal ──────▶ BridgeLink: läuft ohne Stellwerk ─▶ FHIR-CDR
```

Zwei Arten von Schnittstelle, die sich im Betrieb unterscheiden:

| Quelle | Ergebnis | Was im Betrieb läuft |
|---|---|---|
| **Messwerte in einer Datenbank** (eine Zeile je Befund) | je Zeile eine Transaktion mit `Patient` und `Observation` | der Kanal in BridgeLink, ohne Stellwerk |
| **Dokumente** (HL7 MDM, PDF, Textspalten) | Dokument-Bundle mit einer `Composition` nach ISiK | Stellwerk liest und ordnet zu, der Kommunikationsserver reicht durch |

Werte werden nie umgerechnet, Text im FHIR-Dokument kommt immer wörtlich aus der Quelle, und nichts fällt stillschweigend weg.
Grundsätze der Verarbeitung: [CLAUDE.md](CLAUDE.md).

## In fünf Minuten

```bash
pip install -e '.[ocr]'          # ohne [ocr] gehen nur PDFs mit Textebene
cd beispiele                     # erfundene Daten, eigene stellwerk.yaml
stellwerk doctor                 # prüft Texterkennung, lokales Modell, Konfiguration
stellwerk convert arztbrief_mdm.hl7
stellwerk convert arztbrief_scan.pdf --patient-id 4711001 --fall 2026004711 --dokument-id SCAN-1
stellwerk sql kis_zuordnung.yaml
stellwerk serve                  # Workbench auf http://127.0.0.1:8080
```

Je Dokument entsteht ein Ordner unter `out/`:

| Datei | Inhalt |
|---|---|
| `bundle.json` | das FHIR-Bundle für den Kommunikationsserver |
| `bericht.html` | Prüfbericht mit der Zuordnung Zeile für Zeile |
| `bericht.json` | derselbe Bericht für Skripte |
| `original.*` | der Eingang, unverändert |
| `text.txt` | der gelesene Text |

Rückgabewert: `0` OK, `1` zu prüfen, `2` Fehler.

Weitere Eingänge: `POST /v1/convert` (Datei unverändert im Body) und `stellwerk mllp` (HL7 per MLLP, Vorgabe-Port 6661).

## Workbench

`stellwerk serve` öffnet die Workbench im Browser. Links stehen die Projekte als frei gegliederter Baum (Haus, Schnittstelle,
Aufgabe), rechts daneben Quelle, Zuordnung und FHIR-Ausgabe. Eine Aufgabe durchläuft diese Schritte:

1. **Quelle wählen:** Datei hochladen oder eine Datenbank eintragen. Bei Datenbanken schlägt „Abfragen vorschlagen“ fertige
   `SELECT`-Abfragen vor, bei Bedarf mit Verknüpfung zu Fall und Patient (immer `LEFT JOIN`, vermutete Verknüpfungen markiert).
2. **Zuordnen:** Bei Dokumenten wählt man den Zielabschnitt je Überschrift (gilt dann für diese Aufgabe) und kann Kopfdaten
   überschreiben. Beim Feld-Mapping bildet die Mapping-Tabelle die Spalten auf `Patient` und `Observation` ab. Vorschläge
   (nach Spaltennamen, optional vom lokalen Modell) stehen als eigene Zeile mit Begründung und wirken erst nach „Übernehmen“.
   Die Spaltenliste zeigt je Spalte den Stand und, mit einem Klick, Leeranteil, Anzahl verschiedener Werte und die häufigsten Werte.
3. **Prüfen:** Der Reiter „Befunde“ sagt je Punkt, wo er steht, was los ist und was zu tun ist.
4. **Ausgeben:** Bundle laden, in den Abholordner des Kommunikationsservers legen oder, wenn `zustellung.ziel` eingestellt ist, an das Zustellziel senden.
5. **Übergeben:** Das **Übergabepaket** (ZIP) beschreibt die Schnittstelle für den Administrator von Cloverleaf, BridgeLink oder
   Mirth. Für BridgeLink richtet Stellwerk den **fertigen Kanal** selbst ein.

Projekte und Quelldateien liegen im Ordner `arbeitsbereich/` auf dem Rechner, auf dem der Dienst läuft.
Die einfache Upload-Seite gibt es weiter unter `/probieren`.

## Schnittstelle in BridgeLink

Für ein Feld-Mapping aus einer Datenbank erzeugt Stellwerk einen Kanal, der ohne Stellwerk läuft. „Kein Bundle möglich“ heißt:
Patientennummer oder Messwert fehlt.

```
Database Reader ─▶ Bundle je Zeile ─┬─ ohne Prüfpunkte ─▶ FHIR-Ziel (Warteschlange, Wiederholung)
 nur neue Zeilen                    ├─ mit Prüfpunkten ─▶ Prüfablage und Hinweisdatei
                                    └─ kein Bundle möglich ─▶ abgewiesen, Grund in BridgeLink
```

```bash
stellwerk schnittstelle                         # Aufgaben auflisten
stellwerk schnittstelle <Kennung> --einrichten  # in BridgeLink anlegen oder ersetzen
```

Oder in der Workbench unter „Prüfung und Ablage“. Der Kanal baut die Bundles mit derselben Datei, gegen die die Tests jeden
Testfall des Übergabepakets prüfen, und wurde gegen BridgeLink 26.9.0 mit SQLite und PostgreSQL ausprobiert
(Neustart, Warteschlange, Ziel ausgefallen, Zeitumstellung). Was geprüft ist, was nicht und welche Risiken bleiben
(Aufbewahrung von Patientendaten, abgelehnte Bundles, kein TLS am Kanal-Eingang): [docs/bridgelink.md](docs/bridgelink.md).

Das Übergabepaket enthält Abfrage, Zuordnung (CSV, JSON), die Regeln für die Umsetzung und erfundene Testfälle mit erwartetem
Bundle, keine Zeilen aus der Quelle (außer auf Wunsch). `stellwerk vergleichen` prüft, was ein nachgebauter Kanal liefert:
[docs/uebergabe.md](docs/uebergabe.md).

## Was mit schwierigen Eingängen passiert

| Störung | Verhalten |
|---|---|
| `MSH-18` nennt den falschen Zeichensatz | Inhalt wird geprüft, nicht die Angabe; Abweichung wird gemeldet |
| Gemischte Zeichensätze in einer Lieferung | zeilenweise gelesen, als „prüfen“ gemeldet |
| `Ã¤` statt `ä` (auch doppelt) | zurückgerechnet, wenn es eindeutig ist; mit Anzahl und Beispielen gemeldet |
| `M?ller`, `�` | nicht reparierbar; gemeldet, Text bleibt unverändert |
| Zeilenumbruch mitten in `OBX-5` | als Fortsetzung des Feldes gelesen |
| RTF oder HTML in Datenbankfeldern | auf Text reduziert |
| Gescanntes PDF | Texterkennung mit Drehen, Begradigen, Entrauschen; Sicherheit je Zeile |
| Unsicher gelesene Zeile mit Zahlen | einzeln gemeldet (Dosierung, Laborwert, ICD-Code) |
| Geburtsdatum im Text passt nicht zum Patienten | gemeldet: möglicherweise falscher Patient |
| Messwert `<0,6` oder `positiv` | steht unverändert als Text im Bundle und gilt als „zu prüfen“ |

`µg` und `m²` bleiben erhalten; es gibt keine Normalisierung, die Bedeutung ändert.

## Wie Text in den richtigen Abschnitt kommt

1. Liefert die Quelle die Gliederung mit (`OBX-3`, Spaltennamen), gilt die.
2. Sonst sucht Stellwerk Überschriften, die die Vorlage als Alias kennt, auch leicht verlesen (`Medikati0n`).
3. Für den Rest schlägt das lokale Modell einen Abschnitt vor. Das gilt immer als „zu prüfen“.
4. Was übrig bleibt, steht unter „Weitere Angaben“. Stellwerk zählt nach, dass keine Zeile fehlt.

Das Modell gibt nur „Textstück 3 gehört zu Abschnitt verlauf“ zurück und schreibt selbst nichts.
Bestätigt man einen Vorschlag, trägt man die Überschrift als Alias in die Vorlage ein. Der Bericht nennt die Zeile.
Danach läuft derselbe Fall ohne Modell und ohne Rückfrage.

```bash
stellwerk vorlage entlassbrief > entlassbrief.yaml   # anpassen, dann --vorlage entlassbrief.yaml
```

## Datenbank erschließen

```bash
stellwerk discover sqlite:///beispiele/kis.db
```

Liest das ganze Schema (nur lesend), schlägt je Spalte eine Bedeutung mit Begründung vor, zeigt zusammengehörige Felder
(Wert und Einheit, Angaben zur Person, Abschnitte eines Dokuments) und Beziehungen zwischen Tabellen. Was nicht sicher
erkennbar ist, bleibt „ungeklärt“; Entscheidungen von Hand werden gespeichert und haben Vorrang.
Mit `--abfragen` gibt es fertige `SELECT`-Vorschläge aus (in der Workbench: „Abfragen vorschlagen“), mit Kandidaten für die
Spalte, an der der Kanal neue Zeilen erkennt. Das Schema wurde nur an SQLite gelesen. Für Bedeutungen und Beziehungen gibt es
noch keine Oberfläche (`POST /api/erschliessen`).

## Sprachmodell

```yaml
llm:
  modell: <name>               # leer = ohne Modell
  url: http://localhost:11434
  api: ollama                  # oder openai (llama.cpp, vLLM, LM Studio, Gateway, extern)
  extern:
    erlaubt: false             # externes Modell: Daten verlassen das Haus
    daten: metadaten           # metadaten | alles
```

Der API-Schlüssel kommt aus der Umgebungsvariable `STELLWERK_LLM_KEY`, nie aus der Datei.
Ein externes Modell lehnt Stellwerk ab, solange `extern.erlaubt` nicht gesetzt ist. Mit `daten: metadaten` gehen nur Tabellen-
und Spaltennamen hinaus; Zeilenwerte und Dokumenttext nur mit `daten: alles`.
`stellwerk doctor` prüft, ob das Modell erreichbar ist und drei Testtexte richtig zuordnet.
Fällt das Modell aus, läuft die Verarbeitung ohne es weiter und meldet das.

## Ausgabe

Vorgabe ist ein Dokument-Bundle nach `ISiKBerichtBundle` mit einer Composition nach `ISiKBerichtSubSysteme`
(gelesen aus ISiK Basis 6.0.0), dazu Patient und Fall zum Abgleich. Mit `ziel: transaktion` entsteht eine
Transaktion für ein CDR: Patient und Fall werden nur angelegt, wenn sie fehlen; das Dokument wird über seine Nummer ersetzt.
Messwerte aus dem Feld-Mapping gehen immer als Transaktion hinaus; mit Auftragsnummer ersetzt ein erneuter Lauf die Ergebnisse,
statt sie zu verdoppeln.

## Grenzen

**Prüfung**
- Nicht mit dem offiziellen FHIR-Validator geprüft. Die Tests prüfen gegen das FHIR-Grundmodell und gegen die Profilregeln,
  die Stellwerk selbst kennt. Vor dem Einsatz mit `validator_cli` und dem ISiK-Paket validieren.
- Nicht mit einem echten Sprachmodell getestet, nur gegen einen Stellvertreter-Server mit der Ollama- und der OpenAI-Schnittstelle.
- Nicht an einer echten Cloverleaf-Instanz getestet. BridgeLink 26.9.0 ist mit SQLite und PostgreSQL als Quelle geprüft,
  SQL Server, Oracle und MySQL nicht. Das Docker-Image und PostgreSQL als Datenbank des Dienstes sind nicht getestet.

**Umfang**
- Das Feld-Mapping kennt eine feste Liste von Zielfeldern (Patient, Fallbezug, Laborwert mit LOINC, Einheit, Zeitpunkt, Auftragsnummer).
  Es gibt kein freies Regelwerk, keinen Terminologieserver und keine MII-Profilangabe. Die LOINC-Vorschlagsliste ist klein
  (neun Laborwerte) und nicht gegen eine aktuelle LOINC-Version geprüft.
- Das Feld-Mapping erwartet je Messgröße eine Spalte. Zeilenweise gespeicherte Laborwerte (eine Zeile je Analyt) muss die Abfrage
  selbst in Spalten bringen.
- Den fertigen Kanal gibt es nur für BridgeLink und nur für Feld-Mapping aus Datenbanken. Für Dokument-Schnittstellen gibt es keinen
  eigenständigen Kanal: Das Lesen und Zuordnen bleibt bei Stellwerk, und der Dienst darf ohne Anmeldung nicht ins Netz.
  Für Cloverleaf und Mirth ist die Umsetzung Handarbeit nach dem Übergabepaket.
- Der Fall im Bundle trägt nur Aufnahmenummer und Fallart. ISiK erwartet den Bezug auf den Abteilungskontakt; das muss das Zielsystem auflösen.
- Der KDL-Code der Vorlage `entlassbrief` (`AD010103`) ist nicht gegen die aktuelle KDL geprüft.
- Das Original-PDF steht nicht im Bundle, sondern im Ausgabeordner.
- Kopfzeilen, die nur auf einer Folgeseite stehen, werden nicht als Kopfzeile erkannt und landen im Abschnitt.
- Numerische `OBX` werden als Textzeilen übernommen, nicht als Observation.

**Betrieb und Sicherheit**
- Anmeldung über den Identitätsdienst des Hauses (OpenID Connect) mit drei Rollen, siehe [docs/anmeldung.md](docs/anmeldung.md).
  Geprüft gegen Keycloak 26.8.2, nicht gegen AD FS, Entra ID oder das Active Directory eines Hauses. Ohne eingerichtete
  Anmeldung startet der Dienst nur auf `127.0.0.1`.
- Der MLLP-Empfang kennt keine Anmeldung. Die Seite erlaubt in der Content-Security-Policy noch `unsafe-inline`.
- Der Kanal-Eingang von BridgeLink spricht kein TLS. Der erzeugte Kanal enthält das Datenbankpasswort, und BridgeLink speichert
  Zeilen und Bundles (Patientendaten) im Nachrichtenspeicher; der Data Pruner dort ist ab Werk aus
  (siehe [docs/bridgelink.md](docs/bridgelink.md)).

## Datenschutz

Ausgaben und Berichte enthalten Patientendaten. `out/`, `stellwerk.yaml` und Übergabepakete sind von Git ausgenommen.
Die Beispiele und Tests verwenden nur erfundene Daten. Das Laufprotokoll und das Audit-Protokoll enthalten nie Patientendaten
oder Dateinamen, nur Kanal, Systemname, Status, Dauer und Befundcodes.

## Dokumentation

| Thema | Datei |
|---|---|
| Installation im Haus oder in der Cloud, Docker | [docs/betrieb.md](docs/betrieb.md) |
| Anmeldung, Rollen, Dienst-Token | [docs/anmeldung.md](docs/anmeldung.md) |
| Übergabepaket und Abfragevorschläge | [docs/uebergabe.md](docs/uebergabe.md) |
| BridgeLink: Eingangskanal und Schnittstelle je Aufgabe | [docs/bridgelink.md](docs/bridgelink.md) |
| Anbindung an Cloverleaf | [docs/cloverleaf.md](docs/cloverleaf.md) |
| Stand aller Anforderungen und Reihenfolge | [docs/anforderungen.md](docs/anforderungen.md) |

## Entwicklung

```bash
pip install -e '.[dev,ocr]'
python -m pytest
```

Gestaltung der Oberfläche und Grundsätze der Verarbeitung: [CLAUDE.md](CLAUDE.md).
