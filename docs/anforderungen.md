# Anforderungen: Stand und Reihenfolge

Stand 15.11.2026, Version 0.3. „Getestet“ heißt: automatische Tests mit erfundenen Daten in diesem Repo.

| Anforderung | Stand | Was es gibt | Was fehlt |
|---|---|---|---|
| Datenbank automatisch erschließen | teilweise | `stellwerk discover` und `POST /api/erschliessen`: Schema lesen, Bedeutung je Spalte mit Begründung, zusammengehörige Felder, Beziehungen (erklärt und vermutet), Entscheidungen des Menschen werden gespeichert und haben Vorrang; fertige `SELECT`-Vorschläge mit Verknüpfung zu Fall und Patient, gegen die Datenbank gezählt, in der Workbench übernehmbar | Oberfläche für Bedeutungen und Beziehungen; Schemawissen zu bekannten KIS (Orbis, Medico, iMedOne); Test an Oracle/MSSQL/PostgreSQL (nur SQLite getestet); große Schemata (Laufzeit, Blättern) |
| LLM lokal oder extern | teilweise | Ollama und OpenAI-kompatible Server; API-Schlüssel aus Umgebungsvariable; externes Modell nur nach Freigabe, wahlweise nur Metadaten oder alle Daten | Anbieter mit eigener Schnittstelle; Gateway/Schleuse; Test mit echtem Modell |
| Zugriff über MCP bzw. abschirmende Zwischenschicht | offen | – | siehe unten |
| On-Premise oder Cloud | weitgehend | Python-Paket, Dockerfile und Compose (ohne Root, schreibgeschützt), Konfiguration über Umgebungsvariablen, SQLite oder PostgreSQL | Image hier nicht gebaut (keine Registry erreichbar); PostgreSQL nur als Quelle für den BridgeLink-Kanal getestet, nicht als Datenbank des Dienstes; Helm-Chart für Kubernetes |
| Zeitgemäßer Stack | teilweise | FastAPI, SQLAlchemy 2, Alembic-Migrationen, uvicorn | React-Oberfläche (entschieden) |
| Härtung | weitgehend | nur lesende SQL-Abfragen, Pfadprüfung, externes LLM gesperrt, ohne Anmeldung nur auf localhost, Sicherheits-Header (CSP, nosniff, frame-ancestors, HSTS), Größenlimit, keine Passwörter in Datenbankadressen, Freigabeliste für Datenbankrechner, Schutz vor fremden Anfragen (CSRF), https direkt am Dienst, Audit-Protokoll mit Nutzernamen, Container ohne Root | CSP ohne `unsafe-inline` (kommt mit React), Abhängigkeitsprüfung in CI, Anmeldung für den MLLP-Empfang gibt es nicht (Protokoll) |
| Nutzerverwaltung | erledigt (OIDC) | Anmeldung über OpenID Connect (Authorization Code, PKCE, RS256), Rollen `lesen`, `bearbeiten`, `verwalten` aus Gruppen, Sitzungen mit Ablauf, Abmelden auch beim Identitätsdienst, Dienst-Token für Kommunikationsserver, `stellwerk anmeldung`; gegen Keycloak 26.8.2 im Browser getestet, siehe [anmeldung.md](anmeldung.md) | Test gegen AD FS, Entra ID und das AD eines Hauses; Back-Channel-Logout; Rechte je Projekt |
| Routing über BridgeLink | erledigt | Kanal wird von Stellwerk angelegt und bereitgestellt; Routing nach Status (FHIR-Ziel mit Warteschlange oder Prüfablage); Zustand per CLI und `/api/bridgelink/status`; Senden aus der Workbench; gegen BridgeLink 26.9.0 getestet | TLS am Kanal-Eingang (BridgeLink-Listener kann es nicht); Zustand in der Oberfläche |
| Übergabe an den Kommunikationsserver | weitgehend (BridgeLink) | Übergabepaket je Aufgabe: Abfrage, Zuordnung (CSV und JSON), Regeln, erfundene Testfälle mit erwartetem Bundle, echte Zeilen nur auf Wunsch; `stellwerk vergleichen`; siehe [uebergabe.md](uebergabe.md). **Fertiger BridgeLink-Kanal je Feld-Mapping-Schnittstelle und je Dokument-Aufgabe (Ordner → Stellwerk → CDR)** (`stellwerk schnittstelle`, Workbench): liest die Datenbank, nur neue Zeilen, baut Bundles nach den Regeln des Pakets, Warteschlange, Prüfablage, abgewiesene Zeilen mit Grund; gegen BridgeLink 26.9.0 mit SQLite und PostgreSQL getestet, siehe [bridgelink.md](bridgelink.md) | SQL Server, Oracle, MySQL gegen eine echte Datenbank testen; Cloverleaf-Export (braucht ein Beispiel aus einer Site); Eingänge für Ordner/Share, SFTP und FHIR; Laborwerte in Zeilenform |
| Monitoring, Customizing, Alerting | begonnen | Laufprotokoll je Lauf (Workbench, HTTP, MLLP) ohne Patientendaten, `/healthz`, `/readyz` | Kennzahlen je Schnittstelle, Oberfläche, Schwellen, Benachrichtigung |

## Reihenfolge

1. ~~**Unterbau:** FastAPI, Datenbank (Projekte, Aufgaben, Läufe, Audit), Docker.~~ Erledigt in 0.2, siehe [betrieb.md](betrieb.md).

Vorgezogen (Entscheidung vom 14.11.2026): **Übergabe an den Kommunikationsserver.**
a) ~~Übergabepaket je Aufgabe, neutral~~ (erledigt), b) ~~BridgeLink-Kanal je Schnittstelle daraus erzeugen~~ (erledigt für Feld-Mapping aus Datenbanken und für Dokumente aus einem Ordner),
c) weitere Eingänge: SFTP, FHIR (Ordner ist über den Kanal abgedeckt), d) Cloverleaf-Export, sobald ein Beispiel vorliegt. Danach weiter mit:

2. ~~**Anmeldung und Härtung:** OIDC, Rollen, Sitzungen, Sicherheits-Header, Audit-Protokoll.~~ Erledigt in 0.3, siehe [anmeldung.md](anmeldung.md).
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
- **OIDC/AD:** getestet gegen einen nachgebauten Identitätsdienst und gegen Keycloak, nicht gegen AD FS, Entra ID oder das AD eines Hauses.
  Vor dem ersten Einsatz dort mit `stellwerk anmeldung` und einer Probeanmeldung je Rolle prüfen.
- **React:** die gebauten Dateien müssen im Paket liegen, damit die Installation im Krankenhaus ohne Node und Internet geht.
