# Anforderungen: Stand und Reihenfolge

Stand 14.11.2026. „Getestet“ heißt: automatische Tests mit erfundenen Daten in diesem Repo.

| Anforderung | Stand | Was es gibt | Was fehlt |
|---|---|---|---|
| Datenbank automatisch erschließen | teilweise | `stellwerk discover` und `POST /api/erschliessen`: Schema lesen, Bedeutung je Spalte mit Begründung, zusammengehörige Felder, Beziehungen (erklärt und vermutet), Entscheidungen des Menschen werden gespeichert und haben Vorrang | Oberfläche dafür; Test an Oracle/MSSQL/PostgreSQL (nur SQLite getestet); große Schemata (Laufzeit, Blättern) |
| LLM lokal oder extern | teilweise | Ollama und OpenAI-kompatible Server; API-Schlüssel aus Umgebungsvariable; externes Modell nur nach Freigabe, wahlweise nur Metadaten oder alle Daten | Anbieter mit eigener Schnittstelle; Gateway/Schleuse; Test mit echtem Modell |
| Zugriff über MCP bzw. abschirmende Zwischenschicht | offen | – | siehe unten |
| On-Premise oder Cloud | offen | läuft als Python-Paket | Docker-Image, Compose, Konfiguration über Umgebungsvariablen, PostgreSQL |
| Zeitgemäßer Stack | offen | Standardserver, Dateien als Ablage, eine HTML-Seite | FastAPI, Datenbank, React (entschieden) |
| Härtung | offen | nur lesende SQL-Abfragen, Pfadprüfung, externes LLM gesperrt, Dienst bindet an localhost | Anmeldung, Rechte, TLS, Sicherheits-Header, Audit-Protokoll, Geheimnisse, Abhängigkeitsprüfung |
| Nutzerverwaltung | offen | – | OIDC/AD, Rollen, Sitzungen (entschieden) |
| Monitoring, Customizing, Alerting | offen | Prüfbericht je Lauf | Laufhistorie, Kennzahlen je Schnittstelle, Schwellen, Benachrichtigung |

## Reihenfolge

1. **Unterbau:** FastAPI, Datenbank (Projekte, Aufgaben, Läufe, Audit), Docker. Bestehende Schnittstelle bleibt erhalten.
2. **Anmeldung und Härtung:** OIDC, Rollen, Sitzungen, Sicherheits-Header, Audit-Protokoll. Vorher darf der Dienst nicht ins Netz.
3. **React-Oberfläche** im bestehenden Erscheinungsbild, zuerst Workbench und Erschließung.
4. **Monitoring und Alerting** auf der Laufhistorie.
5. **LLM-Schleuse.**

## Zur Zwischenschicht zwischen Klinik und Cloud

MCP ist ein Protokoll, mit dem ein Modell Werkzeuge aufruft. Es ist nicht dafür gebaut, Anfragen an ein Modell durchzuleiten.
Die Abschirmung, um die es geht, leistet eine **Schleuse** (LLM-Gateway) im Netz der Klinik:

- einziger Ausgang zum Cloud-Modell, mit Liste erlaubter Ziele
- prüft je Anfrage die Datenklasse (Metadaten oder Patientendaten) und lehnt ab, was nicht freigegeben ist
- kann Kennungen vor dem Versand ersetzen und in der Antwort zurücksetzen
- protokolliert, wer was wohin geschickt hat

Stellwerk spricht die Schleuse über die OpenAI-kompatible Schnittstelle an (`llm.url`, als Host unter `llm.erlaubte_hosts`).
MCP passt zusätzlich in die andere Richtung: Stellwerk als MCP-Server, damit ein Agent Schema lesen und Mappings vorschlagen kann.

## Offene Entscheidungen und Risiken

- **Patientendaten an ein externes Modell** (`llm.extern.daten: alles`): braucht AV-Vertrag, Freigabe des Datenschutzes und eine Regelung zum Ort der Verarbeitung. Technisch ist es ein Schalter; rechtlich nicht.
- **OIDC/AD:** lässt sich hier nur gegen einen nachgebauten Identitätsdienst testen, nicht gegen das AD eines Hauses.
- **React:** die gebauten Dateien müssen im Paket liegen, damit die Installation im Krankenhaus ohne Node und Internet geht.
