# BridgeLink anbinden

BridgeLink (Innovar Healthcare, Open-Source-Weiterentwicklung von Mirth Connect) übernimmt das Routing der
FHIR-Bundles: Stellwerk liefert, BridgeLink entscheidet anhand von Kopfzeilen, wohin ein Bundle geht, stellt
es in eine Warteschlange und wiederholt, wenn das Ziel nicht antwortet.

Geprüft gegen **BridgeLink 26.9.0** (aus dem Quelltext gebaut, eingebettete Derby-Datenbank).

## Ablauf

```
Stellwerk ──POST, Basic-Auth──▶ BridgeLink-Kanal „Stellwerk FHIR-Eingang“ (HTTP Listener :8095/stellwerk)
                                   │ Routing-Merkmal X-Stellwerk-Status
                                   ├─ ok ──────▶ FHIR-Ziel (HTTP Sender, Warteschlange, Wiederholung jede Minute)
                                   └─ pruefen ─▶ Prüfablage (Ordner im BridgeLink-Server)
```

Ohne `kanal.fhir_ziel` landet alles in einer Ablage.

## Einrichten

1. In `stellwerk.yaml`:

   ```yaml
   zustellung:
     ziel: http://bridgelink:8095/stellwerk
     bei_pruefen: zustellen          # Prüf-Bundles gehen mit, BridgeLink legt sie in die Prüfablage
     benutzer: stellwerk             # Anmeldung am Kanal
   bridgelink:
     api: https://bridgelink:8443/api
     benutzer: stellwerk-admin       # BridgeLink-Konto mit Recht, Kanäle anzulegen
     ca: /etc/stellwerk/bridgelink-ca.pem
     kanal:
       fhir_ziel: https://cdr.haus/fhir
       fhir_auth: basic              # keine | basic | bearer
       fhir_benutzer: bridgelink
       fhir_passwort_env: STELLWERK_CDR_PASSWORT
   ```

2. Passwörter nur als Umgebungsvariablen: `STELLWERK_ZIEL_PASSWORT` (Kanal), `STELLWERK_BRIDGELINK_PASSWORT` (API),
   dazu die unter `fhir_passwort_env` bzw. `fhir_token_env` genannte.
3. `stellwerk bridgelink einrichten` legt den Kanal an oder ersetzt ihn (gleiche ID bei jedem Lauf) und stellt ihn bereit.
4. `stellwerk bridgelink status` zeigt Empfangenes, Abgewiesenes und je Ziel Gesendetes, Fehler und Warteschlange.
   Rückgabewert 2 bei Fehlern oder angehaltenem Kanal, für Überwachungsskripte.
5. `stellwerk bridgelink kanal` gibt nur das Kanal-XML aus, zum Import von Hand im Administrator. Es enthält die Passwörter.

In der Workbench steht unter „Prüfung und Ablage“ die Aktion **An Zustellziel senden**, sobald `zustellung.ziel` eine http(s)-Adresse ist.

## Schnittstelle je Aufgabe (Datenbank → FHIR)

Für eine Aufgabe im Modus „Felder: Spalten → Patient, Observation“ erzeugt Stellwerk einen **fertigen Kanal**. Er läuft
ohne Stellwerk: Stellwerk ist das Entwurfswerkzeug, BridgeLink die Laufzeit.

```
Database Reader ──liest die Abfrage der Aufgabe, nur neue Zeilen, höchstens N Zeilen je Lauf
   │ Skript baut je Zeile das Bundle nach den Regeln aus LIESMICH.md (src/stellwerk/kanal_bauen.js)
   ├─ keine Patientennummer / kein Messwert ──▶ abgewiesen, Grund in der Nachrichtenliste (Spalte GRUND)
   ├─ ohne Prüfpunkte ──▶ FHIR-Ziel (Warteschlange, Wiederholung jede Minute)
   └─ mit Prüfpunkten ──▶ Prüfablage (Bundle) und Prüfhinweise (Textdatei mit Codes und Spaltennamen, ohne Werte)
```

Einrichten in der Workbench unter „Prüfung und Ablage“, Block „Schnittstelle in BridgeLink“, oder auf der Kommandozeile:

```
stellwerk schnittstelle                         # Aufgaben auflisten
stellwerk schnittstelle <Kennung> --einrichten  # in BridgeLink anlegen oder ersetzen und bereitstellen
stellwerk schnittstelle <Kennung> --out kanal.xml   # nur das Kanal-XML schreiben (nur für den Besitzer lesbar, enthält das DB-Passwort)
```

**Neue Zeilen.** Ohne Kriterium baut Stellwerk keinen Kanal. Gewählt wird eine Spalte, deren Wert je Zeile größer wird
(laufende Nummer oder Zeitstempel). Die höchste angenommene Zahl merkt sich der Kanal in einer Datei je Schnittstelle
(`bridgelink.kanal.stand_ordner`, auf dem BridgeLink-Server). Alternativ „alle Zeilen bei jedem Lauf“: nur mit zugeordneter
Auftragsnummer, weil das CDR die Ergebnisse dann aktualisiert statt zu verdoppeln.

**Neu lesen.** Stand-Datei löschen oder einen Startwert eintragen. Ohne Auftragsnummer legt das CDR die Ergebnisse dabei doppelt an.

**Datenbankzugang.** Die JDBC-Adresse leitet Stellwerk aus der Datenbankadresse der Aufgabe ab (SQLite, PostgreSQL, SQL Server,
Oracle, MySQL/MariaDB); die Treiber bringt BridgeLink mit. Das Passwort kommt aus dem `${STELLWERK_DB_…}`-Platzhalter und steht
danach **im Kanal in BridgeLink**. Stellwerk liefert das Kanal-XML nie über die HTTP-Schnittstelle aus.

**Aufbewahrung.** Der Nachrichtenspeicher von BridgeLink enthält die gelesenen Zeilen und die Bundles, also Patientendaten.
Der Kanal setzt Fristen (`bridgelink.kanal.inhalt_tage`, Vorgabe 7, und `metadaten_tage`, Vorgabe 30, ohne Archiv). Sie greifen
erst, wenn der **Data Pruner in BridgeLink eingeschaltet** ist (Einstellungen → Data Pruner); ab Werk ist er aus.
Die Prüfablage ist ein Ordner mit Patientendaten und braucht eine eigene Aufräumregel im Betrieb.

**Änderungen.** Wird die Zuordnung in Stellwerk geändert, den Kanal erneut einrichten. Der Kanal wird ersetzt, der Stand bleibt.

## Routing-Merkmale

| Kopfzeile | Inhalt |
|---|---|
| `X-Stellwerk-Bundle-Typ` | `document` oder `transaction`; Pflicht, sonst weist der Kanal die Anfrage ab |
| `X-Stellwerk-Status` | `ok` oder `pruefen` |
| `X-Stellwerk-Inhalt` | Ressourcenart des Hauptinhalts, z. B. `Composition`, `Observation` |
| `X-Stellwerk-Dokumenttyp` | KDL-Code, falls vorhanden |
| `X-Stellwerk-Quelle` | Name des liefernden Systems oder `Datei`/`Abfrage` |
| `X-Request-ID` | Kennung der Übergabe |

Die Merkmale enthalten keine Patientendaten. BridgeLink zeigt Typ, Status, Quelle und Dokumenttyp als Spalten in der Nachrichtenansicht.

## Was getestet ist

Gegen das laufende BridgeLink 26.9.0:

- Kanal anlegen und erneut einrichten (ersetzen), bereitstellen, Zustand lesen
- OK-Bundle kommt am FHIR-Ziel an, mit Basic-Auth und `application/fhir+json`
- Prüf-Bundle liegt unverändert in der Prüfablage
- FHIR-Ziel nicht erreichbar: Bundle bleibt in der Warteschlange und wird nach Rückkehr des Ziels zugestellt
- falsches Kanal-Passwort: HTTP 401; fehlendes Routing-Merkmal: abgewiesen
- Senden aus der Workbench

Automatische Tests (ohne BridgeLink): Kanal-XML, Kopfzeilen, Zustellung mit Anmeldung, Workbench-Aktion, Auswertung des Zustands.

**Schnittstelle je Aufgabe**, gegen BridgeLink 26.9.0 (Quelltext-Build, Derby), mit dem Beispiel-LIS (SQLite über das mitgelieferte JDBC)
und einer PostgreSQL-16-Datenbank mit `NUMERIC`, `TIMESTAMP` und `DATE`; FHIR-Ziel war ein Stub, der jedes Bundle ganz mitschreibt.
Jedes gelieferte Bundle wurde mit dem verglichen, das die Python-Pipeline aus denselben Zeilen baut (`stellwerk vergleichen`):

- 13 Zeilen SQLite (Nachkommastellen `1,10`, Text `<0,6`, Zeitumstellung Lücke und Überlappung, ISO- und deutsche Datumsform,
  leere Felder, fehlender Fall): alle Bundles gleich, die Zeile ohne Patient abgewiesen mit Grund `KEINE_PATIENTENNUMMER`
- PostgreSQL: `NUMERIC(6,2)` `1.10` bleibt `1.10`, `DATE`, `TIMESTAMP`, Zeitumstellung: gleich
- Textwert: Bundle und Hinweisdatei in der Prüfablage, nicht am FHIR-Ziel
- neue Zeilen nachgelegt: nur sie werden gelesen; Fortschrittsmarke als Zahl und als Zeitstempel (`TIMESTAMP(3)`)
- Kanal erneut einrichten, während Bundles in der Warteschlange stehen: nichts doppelt gelesen, Warteschlange bleibt erhalten
- BridgeLink vollständig stoppen und neu starten, mit einer Zeile, die in der Quelle während des Stillstands entstand:
  der Kanal läuft von allein wieder an, die neue Zeile kommt einmal an, nichts doppelt (mit leerer Warteschlange getestet)
- FHIR-Ziel aus: Bundles stehen in der Warteschlange, nach Rückkehr werden sie zugestellt
- FHIR-Ziel antwortet 422: das Bundle bleibt in der Warteschlange und wird jede Minute neu versucht (siehe Grenzen)
- 2 Zeilen je Lauf bei 13 Zeilen: vollständig, in Reihenfolge, ohne Lücke
- „alle Zeilen bei jedem Lauf“

Automatische Tests (`tests/test_schnittstelle.py`, ohne BridgeLink): Kanal-XML, gültiges JavaScript der drei Skripte, Prüfungen,
JDBC-Adressen und **jeder Testfall des Übergabepakets** durch dieselbe Datei `kanal_bauen.js`, die im Kanal läuft.
Der Test gegen einen echten BridgeLink ist nicht automatisiert; er braucht einen laufenden Server mit Zugriff auf Quelle und Ablage.

**Nicht getestet:** SQL Server, Oracle, MySQL (nur die JDBC-Adresse ist getestet, nicht die Verbindung), große Datenmengen,
LOB-Spalten, mehrere BridgeLink-Knoten, TLS zur Datenbank.

## Grenzen und Risiken

**Schnittstelle je Aufgabe**

- **Ein Bundle, das das CDR dauerhaft ablehnt (HTTP 4xx), blockiert die Warteschlange.** BridgeLink versucht es jede Minute
  neu, die folgenden warten. Das ist gewollt, weil sonst eine spätere Korrektur eine frühere überholen könnte; sichtbar wird es
  als wachsende Warteschlange (`stellwerk bridgelink status`, Dashboard). Der Administrator muss die Nachricht prüfen und entfernen.
- **Abgewiesene Zeilen werden nicht erneut gelesen.** Ergänzt die Quelle später die Patientennummer einer schon gelesenen Zeile,
  kommt sie erst nach „neu lesen“ (Stand zurücksetzen).
- **Das Kriterium für neue Zeilen muss je Zeile steigen.** Eine Zeile, die später mit einem kleineren oder gleichen Wert
  festgeschrieben wird als die letzte gelesene (typisch bei Zeitstempeln und parallelen Transaktionen), wird übersehen.
  Eine laufende Nummer ist sicherer. SQLite-Zeitstempel (Text) und `TIMESTAMPTZ` lehnt Stellwerk ab.
- Die Abfrage wird als Unterabfrage verwendet: kein Fragezeichen darin, bei SQL Server kein `ORDER BY`.
- Der Stand liegt in einer Datei auf dem BridgeLink-Server, nicht in der Datenbank von BridgeLink. Ein Cluster teilt sie nicht.
- Das Datenbankpasswort steht im Kanal. Eigenes Konto mit Leserecht auf die Abfrage verwenden.
- Es gibt nur diese Art Schnittstelle (breite Tabelle, eine Zeile je Befund). Laborwerte in Zeilenform und Dokumente
  (PDF, Textspalten, HL7) brauchen die Leselogik von Stellwerk. Dafür gibt es keinen eigenständigen Kanal, solange Stellwerk keine
  Anmeldung hat: Der Dienst darf nicht ins Netz, und ein Kanal müsste Patientendokumente an ihn schicken.

**Kanal-Eingang („Stellwerk FHIR-Eingang“)**

- **Kein TLS am Kanal.** Der HTTP Listener von BridgeLink spricht nur HTTP. Das Kanal-Passwort und die Bundles gehen
  unverschlüsselt über die Leitung. Stellwerk und BridgeLink daher auf demselben Host oder im selben abgeschotteten
  Segment betreiben, oder einen TLS-Proxy vor den Kanal setzen und `zustellung.ziel` auf https stellen.
- Das Kanal-Passwort und das Passwort zum FHIR-Ziel stehen in der Kanalkonfiguration in BridgeLink. Eigenes Konto mit
  wenigen Rechten verwenden.
- Ein abgewiesenes Bundle (ohne Routing-Merkmal) beantwortet BridgeLink mit HTTP 500, nicht 400.
- BridgeLink verweigert den Start als root. Das ist richtig so; für den Test hier wurde es mit `-Dserver.allowRoot=true`
  in einem Wegwerf-Container übergangen. Im Betrieb ein eigenes Dienstkonto verwenden.
- Das BridgeLink-Konto `admin` mit Passwort `admin` wird nur in einer Übergangsfrist angenommen; vorher ändern.
- Der Kanal leitet nur weiter. Antworten des FHIR-Ziels (OperationOutcome) wertet Stellwerk nicht aus; sie stehen in BridgeLink.
