# Stellwerk

Stellwerk macht aus HL7-v2-Nachrichten, SQL-Abfragen und PDFs eine FHIR-Composition, die ein
Kommunikationsserver wie Cloverleaf fast fertig an ein CDR weiterreichen kann. Dazu gibt es einen
Prüfbericht, der sagt, was zu prüfen ist und was man dann tut.

Stand: Version 0.1, Prototyp. Was getestet ist und was nicht, steht unter [Grenzen](#grenzen).

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

## Workbench

`stellwerk serve` öffnet die Workbench im Browser:

- **Links:** Projekte als Baum, frei gegliedert (Haus, Schnittstelle, Aufgabe).
- **Aufgabe:** Quelle (Datei oder SQL-Abfrage), Zuordnung und FHIR-Bundle stehen nebeneinander.
- **Zuordnung ändern:** Zielabschnitt je Überschrift wählen; das gilt dann für diese Aufgabe. Vorschläge des Modells lassen sich übernehmen oder ablehnen.
- **Kopfdaten:** Werte aus der Quelle je Aufgabe überschreiben.
- **Feld-Mapping (SQL):** Spalten einer Abfrage auf `Patient` und `Observation` abbilden. Vorschläge nach Spaltennamen (und optional vom lokalen Modell) stehen als eigene Zeile mit Begründung und werden erst nach „Übernehmen“ wirksam.
- **Ausgabe:** Bundle laden oder in den Abholordner des Kommunikationsservers legen.

Projekte und Quelldateien liegen im Ordner `arbeitsbereich/` auf dem Rechner, auf dem der Dienst läuft.
Die einfache Upload-Seite gibt es weiter unter `/probieren`.

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

`µg` und `m²` bleiben erhalten; es gibt keine Normalisierung, die Bedeutung ändert.

## Wie Text in den richtigen Abschnitt kommt

1. Liefert die Quelle die Gliederung mit (`OBX-3`, Spaltennamen), gilt die.
2. Sonst sucht Stellwerk Überschriften, die die Vorlage als Alias kennt, auch leicht verlesen (`Medikati0n`).
3. Für den Rest schlägt das lokale Modell einen Abschnitt vor. Das gilt immer als „zu prüfen“.
4. Was übrig bleibt, steht unter „Weitere Angaben“. Stellwerk zählt nach, dass keine Zeile fehlt.

Der Text im FHIR-Dokument kommt immer wörtlich aus der Quelle. Das Modell gibt nur
„Textstück 3 gehört zu Abschnitt verlauf“ zurück und schreibt selbst nichts.

Bestätigt man einen Vorschlag, trägt man die Überschrift als Alias in die Vorlage ein. Der Bericht nennt die Zeile.
Danach läuft derselbe Fall ohne Modell und ohne Rückfrage.

```bash
stellwerk vorlage entlassbrief > entlassbrief.yaml   # anpassen, dann --vorlage entlassbrief.yaml
```

## Lokales Sprachmodell

```yaml
llm:
  modell: <name>               # leer = ohne Modell
  url: http://localhost:11434
  api: ollama                  # oder openai (llama.cpp, vLLM, LM Studio)
```

Adressen außerhalb des lokalen Netzes lehnt Stellwerk ab; Systemproxys und Weiterleitungen werden nicht benutzt.
`stellwerk doctor` prüft, ob das Modell erreichbar ist und drei Testtexte richtig zuordnet.
Fällt das Modell aus, läuft die Verarbeitung ohne es weiter und meldet das.

## Ausgabe

Vorgabe ist ein Dokument-Bundle nach `ISiKBerichtBundle` mit einer Composition nach `ISiKBerichtSubSysteme`
(gelesen aus ISiK Basis 6.0.0), dazu Patient und Fall zum Abgleich. Mit `ziel: transaktion` entsteht eine
Transaktion für ein CDR: Patient und Fall werden nur angelegt, wenn sie fehlen; das Dokument wird über seine Nummer ersetzt.

Anbindung an Cloverleaf: [docs/cloverleaf.md](docs/cloverleaf.md).

## Grenzen

- **Nicht mit dem offiziellen FHIR-Validator geprüft.** Die Tests prüfen gegen das FHIR-Grundmodell und gegen die
  Profilregeln, die Stellwerk selbst kennt. Vor dem Einsatz mit `validator_cli` und dem ISiK-Paket validieren.
- **Nicht mit einem echten Sprachmodell getestet**, nur gegen einen Stellvertreter-Server mit der Ollama- und der OpenAI-Schnittstelle.
- **Nicht an einer echten Cloverleaf-Instanz getestet.**
- Der Fall im Bundle trägt nur Aufnahmenummer und Fallart. ISiK erwartet den Bezug auf den Abteilungskontakt; das muss das Zielsystem auflösen.
- Der KDL-Code der Vorlage `entlassbrief` (`AD010103`) ist nicht gegen die aktuelle KDL geprüft.
- Das Original-PDF steht nicht im Bundle, sondern im Ausgabeordner.
- Kopfzeilen, die nur auf einer Folgeseite stehen, werden nicht als Kopfzeile erkannt und landen im Abschnitt.
- Numerische `OBX` werden als Textzeilen übernommen, nicht als Observation.
- Der HTTP-Dienst und die Workbench haben keine Anmeldung und keine Benutzerverwaltung.
- Das Feld-Mapping kennt eine feste Liste von Zielfeldern (Patient, Fallbezug, Laborwert mit LOINC, Einheit, Zeitpunkt) und erzeugt je Zeile eine Transaktion. Es gibt kein freies Regelwerk, keinen Terminologieserver und keine MII-Profilangabe.
- Werte werden nie umgerechnet. Nicht-numerische Werte wie `<0,6` stehen als Text im Bundle; gemischte Einheiten werden gemeldet.
- Die LOINC-Vorschlagsliste ist klein (neun Laborwerte) und nicht gegen eine aktuelle LOINC-Version geprüft.
- Für Mirth gibt es keinen eigenen Export; es bekommt dasselbe FHIR-Bundle über Abholordner oder HTTP.

## Datenschutz

Ausgaben und Berichte enthalten Patientendaten. `out/` und `stellwerk.yaml` sind von Git ausgenommen.
Die Beispiele und Tests verwenden nur erfundene Daten.

## Entwicklung

```bash
pip install -e '.[dev,ocr]'
python -m pytest
```

Gestaltung der Oberfläche und Grundsätze der Verarbeitung: [CLAUDE.md](CLAUDE.md).
