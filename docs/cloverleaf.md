# Anbindung an Cloverleaf

Nicht an einer echten Instanz getestet. Die drei Wege nutzen nur Dateien, MLLP und HTTP und gelten
genauso für andere Kommunikationsserver.

## 1. MLLP hinein, Datei heraus

Cloverleaf schickt die Dokumentnachricht (`MDM^T02`, `ORU^R01`) über einen ausgehenden MLLP-Thread an Stellwerk
und holt das Bundle über einen eingehenden Datei-Thread ab.

```bash
stellwerk mllp --host 0.0.0.0 --port 6661
```

```yaml
# stellwerk.yaml
zustellung:
  ziel: /daten/cloverleaf/stellwerk_in     # Verzeichnis, das der Datei-Thread abholt
  bei_pruefen: zurueckhalten
```

- Stellwerk antwortet mit `AA`, sobald die Nachricht auf Platte liegt. Rückfragen zum Inhalt stehen im
  Prüfbericht und halten den sendenden Thread nicht an.
- `AE` kommt nur, wenn die Nachricht nicht gespeichert werden konnte. Dann soll Cloverleaf wiederholen.
- Bundles werden erst unter anderem Namen geschrieben und dann umbenannt. Der Abholer sieht keine halben Dateien.
- Dokumente mit Prüfpunkten bleiben in `out/` liegen, bis jemand sie freigibt
  (`stellwerk convert out/eingang/<datei>.hl7 --trotz-pruefung`) oder `bei_pruefen: zustellen` gesetzt ist.

## 2. Aufruf per HTTP

Ein Cloverleaf-Thread schickt die Rohdaten an Stellwerk und bekommt Bundle und Bericht in einer Antwort.

```
POST http://stellwerk-host:8080/v1/convert?vorlage=entlassbrief
Body: die HL7-Nachricht oder das PDF, unverändert

200  {"status": "ok" | "pruefen", "dokumente": [{"bundle": {...}, "befunde": [...]}]}
422  {"status": "fehler", ...}     kein zustellbares Bundle
```

Für PDFs ohne Kopfdaten: `patient_id`, `fall`, `dokument_id` als Parameter mitgeben.
Der Dienst hat keine Anmeldung. Nur im geschützten Netz betreiben.

## 3. Zustellung per HTTP

```yaml
zustellung:
  ziel: https://cloverleaf-host:8443/stellwerk     # oder direkt die Basisadresse des Zielsystems
```

Stellwerk sendet das Bundle als `application/fhir+json`. Bei `ziel: isik-dokument` erwartet ISiK den POST
an die Basisadresse des Zielsystems; kann es Patient oder Fall nicht auflösen, antwortet es mit 422.

## Was Cloverleaf noch tun muss

- Vorläufige Dokumente (`TXA-17`) vorher ausfiltern oder bewusst durchlassen.
- Routing nach Status: `bericht.json` enthält `status` und `befunde` für eigene Regeln.
- Namensräume für Patienten-, Fall- und Dokumentnummer in `stellwerk.yaml` setzen.
