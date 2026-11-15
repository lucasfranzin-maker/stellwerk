# Betrieb

Stand 0.2. Der Dienst hat **noch keine Anmeldung**. Bis OIDC eingebaut ist, nur auf `127.0.0.1` betreiben
oder hinter einem Reverse-Proxy, der selbst anmeldet.

## Im Haus ohne Container

```bash
pip install 'stellwerk[ocr]'              # Python 3.10 oder neuer
apt install tesseract-ocr tesseract-ocr-deu poppler-utils
stellwerk init                            # legt stellwerk.yaml an
stellwerk migrate                         # Datenbankschema anlegen
stellwerk serve                           # Workbench auf http://127.0.0.1:8080
stellwerk mllp --port 6661                # optional: HL7-Empfang
```

Ohne Internet: Pakete vorher mit `pip download 'stellwerk[ocr]' -d pakete/` holen und im Haus mit
`pip install --no-index --find-links pakete/ stellwerk[ocr]` installieren. Schriften und Oberfläche liegen im Paket.

## Mit Docker

```bash
docker build -t stellwerk:0.2 .
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

## Betriebsendpunkte

| Pfad | Zweck |
|---|---|
| `GET /healthz` | Prozess lebt |
| `GET /readyz` | Datenbank erreichbar (503 sonst) |
| `GET /api/laeufe` | Laufprotokoll ohne Patientendaten: Kanal, Quelle, Status, Dauer, Befundcodes |
| `GET /api/audit` | Wer hat was geändert (noch ohne echte Nutzer, siehe Anmeldung) |
| `GET /api/openapi.json` | Beschreibung der Schnittstelle |
