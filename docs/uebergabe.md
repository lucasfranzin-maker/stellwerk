# Übergabe an den Kommunikationsserver

Stellwerk ist das Entwurfswerkzeug: Es erschließt die Datenbank, schlägt Abfrage und Zuordnung vor und zeigt das Ergebnis.
Der Administrator des Kommunikationsservers (Cloverleaf, BridgeLink, Mirth) übernimmt das Ergebnis als **Übergabepaket**
und baut damit den Thread oder Kanal.

## Ablauf

1. **Abfrage vorschlagen lassen.** In der Workbench bei einer SQL-Aufgabe die Datenbank eintragen und „Abfragen vorschlagen“ wählen,
   oder auf der Kommandozeile:

   ```bash
   stellwerk discover sqlite:///beispiele/lis.db --abfragen
   ```

   Je Tabelle mit Messwerten oder Dokumenten entsteht ein fertiges `SELECT`. Fehlen dort Patient oder Fall, verknüpft Stellwerk
   die Stammtabellen, höchstens zwei Schritte weit und immer als `LEFT JOIN`, damit keine Zeile verloren geht.
2. **Vorschlag übernehmen**, Zuordnung der Spalten prüfen und annehmen (wie bisher).
3. **Übergabepaket laden** unter „Prüfung und Ablage“ oder mit `stellwerk uebergabe <Aufgabe>` (ohne Angabe: Liste der Aufgaben).
4. Im Kommunikationsserver nachbauen und mit `stellwerk vergleichen` gegen die Testfälle prüfen.
   **BridgeLink:** Stellwerk erzeugt den fertigen Kanal selbst (`stellwerk schnittstelle`, siehe [bridgelink.md](bridgelink.md)).
   Das Paket bleibt dann die Dokumentation und der Prüfstein; der Kanal baut dieselben Bundles.

## Was ein Vorschlag für eine Abfrage aussagt

| Angabe | Bedeutung |
|---|---|
| erklärter Fremdschlüssel | Die Datenbank selbst kennt die Beziehung. |
| VERMUTET | Gleiche Bedeutung und überlappende Werte in der Stichprobe. Vor der Übernahme am Datenmodell prüfen. |
| „n von m Zeilen finden keinen Eintrag in …“ | Diese Zeilen bleiben in der Ausgabe, aber ohne die Angaben aus der Stammtabelle. Ohne Patientennummer entsteht für sie kein Bundle. |
| „Die Abfrage liefert x Zeilen, die Tabelle hat y“ | Eine Verknüpfung vervielfacht Zeilen; die Beziehung stimmt so nicht. |

Jeder Vorschlag wird einmal gegen die Datenbank gezählt. Rückgabewert der Kommandozeile: `1`, wenn etwas zu prüfen ist.
Bewegungsdaten werden nie miteinander verknüpft (Befunde nicht mit Briefen), nur mit Patientenstamm und Fällen.

## Inhalt des Pakets: Messwerte (Feld-Mapping)

| Datei | Zweck |
|---|---|
| `LIESMICH.md` | Quelle, Ziel, Zuordnung, die Regeln für die Umsetzung, offene Punkte |
| `abfrage.sql` | die Abfrage, wie sie in Stellwerk geprüft wurde |
| `zuordnung.csv` | je Spalte: Stand, FHIR-Pfad, LOINC, Einheit (für Excel) |
| `zuordnung.json` | dasselbe maschinenlesbar, dazu Namensräume, Zeitzone, Geschlechtscodes |
| `testfaelle/` | erfundene Zeilen und das Bundle, das daraus entstehen muss |
| `echte-zeilen/` | nur auf Wunsch: die ersten fünf Zeilen der Abfrage mit erwartetem Bundle |

Die Testfälle decken ab: vollständige Zeile, leere Angaben, Wert ohne Zahl (`<0,6`), unbekannter Code, fehlende
Patientennummer, Zeile ohne Messwert, Winter- und Sommerzeit. `erwartet.json` mit dem Inhalt `null` heißt: kein Bundle.

Vorschläge, die niemand angenommen hat, stehen als „Vorschlag, nicht angenommen“ in der Zuordnung und in keinem erwarteten Bundle.
Hat die Zuordnung noch Fehler, gibt es kein Paket.

**Zeilenform.** Bei Aufgaben mit Analysecode enthält das Paket zusätzlich die Codetabelle (in `LIESMICH.md` und als `codes` an der
Codespalte in `zuordnung.json`) und den Testfall `07-code-nicht-in-der-tabelle`. Die Regeln beschreiben, wie der Code nachzuschlagen ist.

## Inhalt des Pakets: Dokumente

Lesen, Zeichensatz reparieren und Abschnitte zuordnen bleibt bei Stellwerk; das kann kein Kanal-Skript.
Das Paket enthält `LIESMICH.md` mit dem Aufruf (`POST /v1/convert`), die Vorlage mit den in der Aufgabe ergänzten Überschriften
und bei SQL-Aufgaben die Abfrage.

## Vergleichen

```bash
stellwerk vergleichen testfaelle/01-vollstaendig/erwartet.json ausgabe-des-servers.json
```

Rückgabewert `0` bei Gleichheit, sonst `1` mit einer Zeile je Abweichung. Nicht verglichen werden die Reihenfolge der Einträge
und die Werte der `fullUrl` (die Verweise müssen stimmen). Zeitpunkte gelten als gleich, wenn sie denselben Augenblick meinen.
Verlorene Nachkommastellen (`1.1` statt `1.10`) sind eine Abweichung.

## Datenschutz

- Ohne den Schalter enthält das Paket keine Zeilen und keine einzelnen Werte aus der Quelle. Aus den echten Daten stammen
  Spaltennamen, die Schreibweise von Datum und Dezimalzeichen und, unter „Offene Punkte“, Einheiten und Codes mit ihrer Häufigkeit.
- Mit „Echte Zeilen beilegen“ bzw. `--mit-daten` enthält es Patientendaten. Der Dateiname endet dann auf `-mit-patientendaten.zip`.
- Jedes erzeugte Paket steht im Audit-Protokoll (`uebergabe.erzeugt`), mit der Angabe, ob Patientendaten enthalten sind.
- Steht in der Abfrage selbst ein Patientenbezug (feste Patientennummer), geht er mit ins Paket.
- `uebergabe-*.zip` ist in `.gitignore` eingetragen.

## Was getestet ist

- Vorschläge und Paket an den Beispieldatenbanken `beispiele/kis.db` und `beispiele/lis.db` (SQLite, erfundene Daten).
- Die Regeln im Paket reichen zum Nachbau: `tests/nachbau.js` kennt nur `LIESMICH.md` und `zuordnung.json` und trifft alle
  erwarteten Bundles (Test läuft, wenn `node` installiert ist).
- Knopf und Vorschläge in der Workbench im Browser.
- Der erzeugte BridgeLink-Kanal baut für jeden Testfall des Pakets dasselbe Bundle (`tests/test_schnittstelle.py`), und im echten
  BridgeLink dasselbe wie die Python-Pipeline (siehe [bridgelink.md](bridgelink.md)).

## Grenzen

- **Cloverleaf und Mirth: kein fertiger Thread oder Kanal.** Das Paket ist neutral; die Umsetzung dort ist Handarbeit nach den Regeln.
  Für BridgeLink gibt es den erzeugten Kanal. Für Cloverleaf braucht es ein Beispiel aus einer echten Site.
- **Zeilenform braucht eine Codetabelle, die Sie pflegen.** Liegen Laborwerte zeilenweise vor (eine Zeile je Ergebnis mit
  Analysecode, Wert, Einheit), der Spalte mit dem Code das Ziel „Analysecode“ geben und die Codetabelle füllen (lokaler Code,
  LOINC, Anzeige, feste Einheit). Stellwerk schlägt dafür keine LOINC-Codes vor und rät keinen. Codes, die in der Tabelle fehlen,
  erzeugen keine Observation und stehen als Prüfpunkt im Bericht. Genau eine Wertspalte, höchstens eine Einheitenspalte. Pro
  Zeile entsteht ein eigenes Bundle (Patient und eine Observation); mehrere Zeilen eines Auftrags werden also einzeln zugestellt
  und über `Auftragsnummer-LOINC` wieder zusammengeführt.
- Vorschläge für Abfragen nur an SQLite getestet. Bezeichner werden nach SQL-Standard geschützt; Eigenheiten von Oracle
  oder MSSQL (Schema-Präfix, Groß- und Kleinschreibung) sind nicht geprüft.
- Die Stichprobe für vermutete Beziehungen umfasst 200 Zeilen je Tabelle.
- Die Abfrage im Paket hat kein Kriterium für neue Zeilen; das hängt vom Kommunikationsserver ab. Der BridgeLink-Kanal ergänzt es
  (Spalte, die je Zeile steigt, siehe [bridgelink.md](bridgelink.md)).
- Das Skript `tests/nachbau.js` stammt vom selben Verfasser wie die Regeln. Es zeigt, dass die Regeln vollständig sind,
  nicht, dass ein Dritter sie beim ersten Lesen richtig versteht.
