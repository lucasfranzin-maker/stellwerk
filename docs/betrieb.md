# Betrieb

Stand 0.3. Ohne eingerichtete Anmeldung startet der Dienst **nur auf `127.0.0.1`**. Fürs Netz die Anmeldung über den
Identitätsdienst des Hauses einrichten: [anmeldung.md](anmeldung.md).

## Im Haus ohne Container

```bash
pip install 'stellwerk[ocr]'              # Python 3.10 oder neuer
apt install tesseract-ocr tesseract-ocr-deu poppler-utils
stellwerk init                            # legt stellwerk.yaml an
stellwerk migrate                         # Datenbankschema anlegen
stellwerk serve                           # Workbench auf http://127.0.0.1:8080
stellwerk mllp --port 6661                # optional: HL7-Empfang
```

Im Netz, mit Anmeldung und https (Zertifikat des Hauses):

```bash
stellwerk anmeldung                       # Einstellungen prüfen, nennt die Adressen für den Identitätsdienst
stellwerk serve --host 0.0.0.0 --port 8443 --tls-zertifikat cert.pem --tls-schluessel key.pem
```

Der MLLP-Empfang kennt keine Anmeldung. Ihn nur in einem geschützten Netz oder hinter dem Kommunikationsserver betreiben.

Ohne Internet: Pakete vorher mit `pip download 'stellwerk[ocr]' -d pakete/` holen und im Haus mit
`pip install --no-index --find-links pakete/ stellwerk[ocr]` installieren. Schriften und Oberfläche liegen im Paket.

## Mit Docker

```bash
docker build -t stellwerk:0.3 .
docker compose up -d                                        # SQLite im Volume
POSTGRES_PASSWORD=... docker compose --profile postgres up -d   # mit PostgreSQL
```

Das Image läuft ohne Root, mit schreibgeschütztem Dateisystem außer `/daten`, ohne Linux-Capabilities.
Die Compose-Datei veröffentlicht den Port nur auf `127.0.0.1`.

**Nicht getestet:** Das Image ließ sich in der Entwicklungsumgebung nicht bauen, weil dort keine Container-Registry
erreichbar war. Vor dem ersten Einsatz einmal bauen und starten.

## Datenbank

| Einsatz | Adresse |
|---|---|
| Im Haus, ein Server | `sqlite:///arbeitsbereich/stellwerk.db` (Vorgabe) |
| Cloud, mehrere Instanzen | `postgresql+psycopg://stellwerk:${STELLWERK_DB_PG}@host:5432/stellwerk` |

Gesetzt über `datenbank` in `stellwerk.yaml` oder `STELLWERK_DATENBANK`. Das Schema wird beim Start auf den
neuesten Stand gebracht. Für die Datenbankverwaltung gibt `stellwerk migrate --sql postgresql` das SQL aus.

**Nicht getestet:** PostgreSQL. Getestet ist SQLite; für PostgreSQL ist nur das erzeugte SQL geprüft.

Ein Arbeitsbereich aus Version 0.1 (`baum.json`, `aufgaben/`) wird beim ersten Start übernommen, wenn die Datenbank leer ist.

## Passwörter und Schlüssel

- Nie in `stellwerk.yaml`, nie in Aufgaben. Stellwerk lehnt Datenbankadressen mit Passwort ab.
- Stattdessen Platzhalter: `oracle+oracledb://kis_lesen:${STELLWERK_DB_KIS}@kis-db:1521/?service_name=KISP`
  und `STELLWERK_DB_KIS` als Umgebungsvariable (oder Docker-Secret) des Dienstes setzen.
- LLM-Schlüssel: `STELLWERK_LLM_KEY`.
- Für Quellsysteme ein Datenbankkonto **nur mit Leserechten** verwenden. Stellwerk prüft zusätzlich, dass nur `SELECT` ausgeführt wird.

## Umgebungsvariablen

| Variable | Bedeutung |
|---|---|
| `STELLWERK_CONFIG` | Pfad zur `stellwerk.yaml` |
| `STELLWERK_DATENBANK` | Datenbankadresse von Stellwerk |
| `STELLWERK_ARBEITSBEREICH`, `STELLWERK_AUSGABE` | Ordner für Daten und Ausgabe |
| `STELLWERK_SYSTEM_PATIENT`, `_FALL`, `_DOKUMENT` | Namensräume der Identifikatoren |
| `STELLWERK_LLM_URL`, `STELLWERK_LLM_MODELL`, `STELLWERK_LLM_API`, `STELLWERK_LLM_KEY` | Sprachmodell |
| `STELLWERK_DB_*` | Passwörter für Datenbankadressen (Platzhalter) |
| `STELLWERK_PROXY_IPS` | Adressen vertrauenswürdiger Reverse-Proxys (Vorgabe `127.0.0.1`) |
| `STELLWERK_OIDC_SECRET` | Geheimnis des Clients beim Identitätsdienst |
| `STELLWERK_DIENST_TOKEN` | Zugang für Kommunikationsserver zu `/v1/convert` (mindestens 32 Zeichen) |
| `STELLWERK_OHNE_ANMELDUNG` | `1` erlaubt den Start ohne Anmeldung außerhalb von `127.0.0.1` (nur hinter einem Proxy, der selbst anmeldet, oder wenn der Port nur lokal veröffentlicht ist) |

## Betriebsendpunkte

| Pfad | Zweck |
|---|---|
| `GET /healthz` | Prozess lebt |
| `GET /readyz` | Datenbank erreichbar (503 sonst) |
| `GET /api/laeufe` | Laufprotokoll ohne Patientendaten: Kanal, Quelle, Status, Dauer, Befundcodes |
| `GET /api/audit` | Wer hat was geändert, mit dem Namen aus der Anmeldung (Rolle `verwalten`) |
| `GET /api/openapi.json` | Beschreibung der Schnittstelle |

## Betrieb beobachten

In der Workbench zeigt „Betrieb“ (links unten) je Kanal und Quelle die Läufe der letzten 24 Stunden, 7 oder 30 Tage: Zahl der Läufe, in
Ordnung, zu prüfen, Fehler, Median und 95. Perzentil der Dauer, letzter Lauf und die häufigsten Befundcodes. Dieselben Zahlen liefert
`GET /api/kennzahlen?stunden=…` (Rolle `lesen`). Es sind nur Läufe von Stellwerk selbst (Workbench, `/v1/convert`, MLLP); die Kanäle in
BridgeLink beobachtet man im Dashboard von BridgeLink. Schwellen und Benachrichtigungen gibt es noch nicht.
