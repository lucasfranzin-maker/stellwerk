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

## Grenzen und Risiken

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
