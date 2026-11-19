# Anmeldung und Rechte

Stellwerk speichert keine Passwörter. Nutzer melden sich beim Identitätsdienst des Hauses an (OpenID Connect:
Keycloak, AD FS, Entra ID). Von dort kommen Kennung, Name und Gruppen. Stellwerk bildet die Gruppen auf drei Rollen ab
und führt die Sitzung selbst.

Ohne Einrichtung (`anmeldung.art: keine`) startet der Dienst nur auf `127.0.0.1`. Das ist für den eigenen Rechner gedacht.

## Einrichten

1. **Im Identitätsdienst** einen Client für Stellwerk anlegen: Ablauf „Authorization Code“, PKCE mit S256,
   Signatur des ID-Tokens RS256. Die Gruppen des Nutzers müssen als Claim im ID-Token oder in den Nutzerangaben stehen.
2. **In `stellwerk.yaml`:**

   ```yaml
   anmeldung:
     art: oidc
     oeffentliche_adresse: https://stellwerk.haus       # so rufen Nutzer Stellwerk auf
     oidc:
       aussteller: https://login.haus/realms/klinik     # genau wie der Dienst sich selbst nennt
       client_id: stellwerk
       rollen_claim: groups
     rollen:
       lesen: [stellwerk-lesen]
       bearbeiten: [stellwerk-entwicklung]
       verwalten: [stellwerk-admin]
   quellen:
     erlaubte_hosts: [kis-db.haus, lis-db.haus:1521]    # siehe „Datenbanken freigeben“
   ```

   Das Geheimnis des Clients steht nicht in der Datei, sondern in der Umgebungsvariable `STELLWERK_OIDC_SECRET`.
   Ohne Geheimnis arbeitet Stellwerk als öffentlicher Client mit PKCE.
3. **Prüfen:** `stellwerk anmeldung` fragt den Identitätsdienst ab und nennt die beiden Adressen, die dort einzutragen sind
   (Redirect-URI `…/anmelden/antwort`, nach dem Abmelden `…/abgemeldet`).
4. **Starten** mit https: entweder hinter einem Reverse-Proxy oder direkt mit
   `stellwerk serve --host 0.0.0.0 --tls-zertifikat cert.pem --tls-schluessel key.pem`.

## Rollen

Jede Rolle schließt die davor ein. Wer in keiner zugeordneten Gruppe ist, kommt nicht hinein.

| Rolle | Darf |
|---|---|
| `lesen` | Projekte, Aufgaben, Zuordnungen, Laufprotokoll und Zustand der Zustellung ansehen |
| `bearbeiten` | Aufgaben anlegen und ändern, Quellen hochladen, ausführen, Datenbanken erschließen, Bundles in die Ablage legen, Übergabepaket ohne echte Zeilen, Dateien über `/v1/convert` umwandeln |
| `verwalten` | Bundles ans CDR senden, Kanäle in BridgeLink einrichten, Übergabepaket mit echten Zeilen, Audit-Protokoll lesen, Datenbanken auf nicht freigegebenen Rechnern eintragen (nur ohne Freigabeliste) |

Alle drei Rollen sehen Patientendaten, sobald eine Aufgabe welche enthält. `lesen` ist keine Rolle für Unbeteiligte.

Die Regeln stehen an einer Stelle (`auth.REGELN`). Ein Pfad, der dort fehlt, ist nur für `verwalten` erreichbar; ein Test
schlägt an, wenn ein neuer Pfad keine Regel hat.

## Datenbanken freigeben

Der Dienst verbindet sich mit Passwörtern aus seiner Umgebung (`${STELLWERK_DB_…}`). Eine Adresse, die auf einen fremden
Rechner zeigt, würde ihm dieses Passwort schicken. Deshalb:

- Mit `quellen.erlaubte_hosts` sind nur diese Rechner (oder `rechner:port`) erlaubt, für alle Rollen. Parameter in der
  Adresse, die das Ziel ändern (`host`, `odbc_connect` …), werden abgelehnt.
- Ohne Liste darf bei eingeschalteter Anmeldung nur `verwalten` eine Datenbank im Netz eintragen.
- SQLite-Dateien auf dem Stellwerk-Server sind davon nicht betroffen.

## Kommunikationsserver

Maschinen melden sich nicht über den Browser an. Für `POST /v1/convert` und `GET /v1/health` gilt ein Dienst-Token
(mindestens 32 Zeichen) aus der Umgebungsvariable `STELLWERK_DIENST_TOKEN`:

```
Authorization: Bearer <Token>
```

Erzeugen: `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Das Token gilt nur für `/v1/…`, nie für die Workbench.

## Sitzungen

- Dauer: 8 Stunden, danach neu anmelden. Ohne Anfrage endet die Sitzung nach 60 Minuten
  (`anmeldung.sitzung_stunden`, `anmeldung.leerlauf_minuten`).
- Im Cookie steht ein zufälliger Wert, in der Datenbank nur dessen SHA-256. Das Cookie ist `HttpOnly`, `SameSite=Lax`,
  bei https zusätzlich `Secure` und an den Rechnernamen gebunden (`__Host-`).
- „Abmelden“ beendet die Sitzung in Stellwerk und beim Identitätsdienst.
- Wird ein Konto im Identitätsdienst gesperrt, läuft eine bestehende Sitzung bis zu ihrem Ende weiter. Sofort beenden:
  `stellwerk anmeldung --beenden <name>` (oder `alle`); `stellwerk anmeldung --sitzungen` zeigt, wer angemeldet ist.
- Anmeldungen, Ablehnungen und Abmeldungen stehen mit dem Namen im Audit-Protokoll.

## Was geprüft wird

- ID-Token: Signatur RS256 gegen die Schlüssel des Ausstellers (mindestens 2048 Bit), Aussteller, Empfänger (`aud`, bei
  mehreren auch `azp`), Ablauf, Nonce. Token mit `none`, HS256 oder einem anderen Verfahren werden abgelehnt.
- Antwort des Identitätsdienstes: `state`, einmalige Verwendung, PKCE.
- Der Aussteller in der Beschreibung (`/.well-known/openid-configuration`) muss dem eingetragenen entsprechen. Alle Adressen
  des Identitätsdienstes müssen https sein (http nur für `localhost`).
- Ändernde Anfragen mit Cookie müssen aus der Stellwerk-Oberfläche kommen (eigener Kopf und Herkunft), als Schutz vor
  fremden Seiten, die im Namen eines angemeldeten Nutzers Anfragen abschicken.

## Hinweise je Identitätsdienst

| Dienst | Aussteller | Gruppen |
|---|---|---|
| Keycloak | `https://<host>/realms/<realm>` | Mapper „Group Membership“ am Client, Claim `groups`, „Full group path“ aus. Realm-Rollen: `rollen_claim: realm_access.roles` |
| AD FS | `https://<host>/adfs` | Regel, die Gruppen als Claim ausgibt; den Namen des Claims unter `rollen_claim` eintragen |
| Entra ID | `https://login.microsoftonline.com/<tenant>/v2.0` | App-Rollen (`rollen_claim: roles`) oder Gruppen-Claim; Gruppen kommen dort als Kennungen (GUID), nicht als Namen |

## Stand der Prüfung

- **Geprüft:** automatische Tests gegen einen nachgebauten Identitätsdienst (auch mit falschen Token), und der ganze Ablauf
  im Browser gegen **Keycloak 26.8.2**: Anmelden, Rollen aus Gruppen, Abmelden bei Stellwerk und Keycloak, Nutzer ohne Gruppe,
  anderes Konto, Dienst-Token.
- **Nicht geprüft:** AD FS, Entra ID und das Active Directory eines Hauses. Die Angaben in der Tabelle oben folgen deren
  Dokumentation, nicht einem Test.
- **Grenzen:** nur RS256. Kein Back-Channel-Logout (siehe „Sitzungen“). Der MLLP-Empfang (`stellwerk mllp`) kennt keine
  Anmeldung; MLLP sieht keine vor. Ihn nur in einem geschützten Netz oder hinter dem Kommunikationsserver betreiben.
  Die Seite ist noch eine Datei mit eingebettetem Skript, deshalb erlaubt die Content-Security-Policy `unsafe-inline`.
