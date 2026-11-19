"""Anmeldung über OpenID Connect, Rollen, Sitzungen, Dienst-Token. Gegen den nachgebauten Identitätsdienst in idp.py."""
import copy
import hashlib
import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest
from idp import Idp, rs256, rsa_schluessel

from stellwerk import auth, config

K = {"X-Stellwerk-Anfrage": "1"}                    # diesen Kopf setzt die Oberfläche an jeder ändernden Anfrage
TOKEN = "t" * 40


@pytest.fixture(scope="session")
def schluessel():
    return rsa_schluessel(), rsa_schluessel()


@pytest.fixture
def idp(schluessel):
    i = Idp(*schluessel)
    yield i
    i.stop()


@pytest.fixture
def acfg(idp, tmp_path, monkeypatch):
    monkeypatch.setenv("STELLWERK_OIDC_SECRET", idp.client_secret)
    monkeypatch.setenv("STELLWERK_DIENST_TOKEN", TOKEN)
    c = copy.deepcopy(config.DEFAULTS)
    c["arbeitsbereich"] = ""
    c["datenbank"] = f"sqlite:///{tmp_path / 'db' / 'stellwerk.db'}"
    c["ausgabe"] = str(tmp_path / "out")
    c["anmeldung"].update(art="oidc", oeffentliche_adresse="http://localhost",
                          rollen={"lesen": ["stellwerk-lesen"], "bearbeiten": ["Stellwerk-Bearbeiten"], "verwalten": ["stellwerk-verwalten"]})
    c["anmeldung"]["oidc"].update(aussteller=idp.url, client_id="stellwerk")
    return c


@pytest.fixture
def c(acfg):
    from fastapi.testclient import TestClient

    from stellwerk.app import create_app
    with TestClient(create_app(acfg), base_url="http://localhost", follow_redirects=False) as cl:
        yield cl


def anmelden(c, idp, gruppen=("stellwerk-bearbeiten",), name="m.muster", ziel="/", **mehr):
    idp.nutzer = {"sub": "u-" + name, "preferred_username": name, **({"groups": list(gruppen)} if gruppen is not None else {}), **mehr}
    r = c.get("/anmelden", params={"ziel": ziel})
    assert r.status_code == 302 and r.headers["location"].startswith(idp.url + "/auth?")
    u = urlsplit(idp.besuchen(r.headers["location"]))
    assert f"{u.scheme}://{u.netloc}{u.path}" == "http://localhost/anmelden/antwort"
    return c.get(u.path + "?" + u.query)


def test_ohne_anmeldung_ist_nichts_erreichbar(c):
    r = c.get("/api/baum")
    assert r.status_code == 401 and r.json() == {"fehler": "Nicht angemeldet.", "anmelden": "/anmelden"}
    r = c.get("/", headers={"Accept": "text/html"})
    assert r.status_code == 302 and r.headers["location"] == "/anmelden?ziel=%2F"
    assert c.put("/api/baum", json={"kinder": []}, headers=K).status_code == 401
    assert c.post("/v1/convert", content=b"MSH|").status_code == 401
    assert c.get("/api/audit").status_code == 401
    assert c.get("/gibt-es-nicht").status_code == 401            # unbekannte Pfade verraten nichts
    assert c.get("/healthz").status_code == 200 and c.get("/readyz").status_code == 200
    assert c.get("/fonts/barlow-semi-condensed-latin-400-normal.woff2").status_code == 200
    assert c.get("/api/ich").json() == {"anmeldung": "oidc", "angemeldet": False, "name": "", "rollen": []}


def test_jede_route_hat_eine_regel(c):
    """Neue Pfade müssen in auth.REGELN stehen. Was fehlt, ist nur für „verwalten“ erreichbar; das soll auffallen."""
    ohne = []
    for route in c.app.routes:
        pfad = re.sub(r"\{[^}]+\}", "x", route.path)
        for m in route.methods - {"HEAD", "OPTIONS"}:
            if not any(mm == m and rx.match(pfad) for mm, rx, _ in auth._REGELN):
                ohne.append(f"{m} {route.path}")
    assert ohne == []
    assert auth.rolle_fuer("DELETE", "/api/baum") == "verwalten" and auth.rolle_fuer("GET", "/api/neu") == "verwalten"
    assert auth.rolle_fuer("HEAD", "/healthz") == auth.OFFEN


def test_anmelden_und_rollen(c, idp):
    r = anmelden(c, idp, ["stellwerk-lesen"], name="l.leser")
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert c.get("/api/ich").json() == {"anmeldung": "oidc", "angemeldet": True, "name": "l.leser", "rollen": ["lesen"]}
    assert c.get("/api/baum").status_code == 200 and c.get("/").status_code == 200
    r = c.put("/api/baum", json={"kinder": []}, headers=K)
    assert r.status_code == 403 and "bearbeiten" in r.json()["fehler"]
    assert c.get("/api/audit").status_code == 403

    anmelden(c, idp, ["STELLWERK-BEARBEITEN"], name="b.bauer")          # Gruppennamen ohne Groß-/Kleinschreibung
    assert c.get("/api/ich").json()["rollen"] == ["lesen", "bearbeiten"]
    assert c.put("/api/baum", json={"kinder": [{"id": "task000001", "typ": "aufgabe", "name": "A"}]}, headers=K).status_code == 200
    assert c.post("/api/aufgabe/task000001/senden", headers=K).status_code == 403
    assert c.post("/api/aufgabe/task000001/kanal", headers=K).status_code == 403
    assert c.get("/api/aufgabe/task000001/uebergabe", params={"daten": 1}).status_code == 403     # echte Zeilen: nur verwalten
    assert c.get("/api/audit").status_code == 403

    anmelden(c, idp, ["irgendwas", "stellwerk-verwalten"], name="v.vogt")
    assert c.get("/api/ich").json()["rollen"] == ["lesen", "bearbeiten", "verwalten"]
    eintraege = c.get("/api/audit").json()["eintraege"]
    assert {(e["wer"], e["aktion"]) for e in eintraege} >= {("l.leser", "anmeldung.erfolgreich"), ("b.bauer", "baum.gespeichert"),
                                                              ("v.vogt", "anmeldung.erfolgreich")}


def test_anfrage_an_den_identitaetsdienst(c, idp):
    anmelden(c, idp)
    q = idp.letzte_anfrage
    assert q["response_type"] == "code" and q["client_id"] == "stellwerk" and q["redirect_uri"] == "http://localhost/anmelden/antwort"
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43 and "openid" in q["scope"].split()
    assert len(q["state"]) >= 32 and len(q["nonce"]) >= 32 and q["state"] != q["nonce"]


def test_nur_die_oberflaeche_darf_aendern(c, idp):
    """Schutz vor fremden Seiten, die im Namen eines angemeldeten Nutzers Anfragen abschicken (CSRF)."""
    anmelden(c, idp)
    baum = {"kinder": []}
    assert c.put("/api/baum", json=baum).status_code == 403
    assert c.put("/api/baum", json=baum, headers={**K, "Origin": "https://fremd.example"}).status_code == 403
    assert c.put("/api/baum", json=baum, headers={**K, "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.put("/api/baum", json=baum, headers={**K, "Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"}).status_code == 200
    assert c.get("/api/baum", headers={"Origin": "https://fremd.example"}).status_code == 200     # Lesen ändert nichts


@pytest.mark.parametrize("fehler, meldung", [
    ({"alg": "none"}, "RS256"),
    ({"alg": "HS256"}, "RS256"),
    ({"fremder_schluessel": True}, "Signatur"),
    ({"inhalt_geaendert": True}, "Signatur"),
    ({"iss": "https://anderer.example"}, "anderen Aussteller"),
    ({"aud": "andere-anwendung"}, "nicht für Stellwerk"),
    ({"aud": ["stellwerk", "andere"]}, "nicht für Stellwerk"),
    ({"aud": ["stellwerk", "andere"], "azp": "andere"}, "nicht für Stellwerk"),
    ({"exp": -3600}, "abgelaufen"),
    ({"nonce": "x" * 43}, "nicht zu dieser Anmeldung"),
])
def test_falsche_token_ergeben_keine_sitzung(c, idp, fehler, meldung):
    idp.fehler = fehler
    r = anmelden(c, idp, ["stellwerk-verwalten"])
    assert r.status_code == 400 and meldung in r.text
    assert c.get("/api/baum").status_code == 401
    assert c.app.state.store.audit_log() == []


def test_mehrere_empfaenger_mit_azp(c, idp):
    idp.fehler = {"aud": ["stellwerk", "andere"], "azp": "stellwerk"}
    assert anmelden(c, idp).status_code == 303


def test_antwort_gilt_einmal_und_nur_mit_passendem_state(c, idp):
    idp.nutzer = {"sub": "u1", "preferred_username": "m", "groups": ["stellwerk-lesen"]}
    u = urlsplit(idp.besuchen(c.get("/anmelden").headers["location"]))
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert "gehört nicht zu dieser Anmeldung" in c.get("/anmelden/antwort", params={**q, "state": "x" * 43}).text
    assert "abgelaufen" in c.get("/anmelden/antwort", params=q).text            # der Versuch ist nach dem ersten Mal verbraucht
    c.get("/anmelden")
    assert c.get("/anmelden/antwort", params={**q, "state": "ü" * 43}).status_code == 400      # Sonderzeichen sind kein Serverfehler
    c.cookies.set("stellwerk-anmeldung", "kein-json")
    assert c.get("/anmelden/antwort", params=q).status_code == 400
    c.cookies.clear()

    u = urlsplit(idp.besuchen(c.get("/anmelden").headers["location"]))
    offen = c.cookies.get("stellwerk-anmeldung")
    assert c.get(u.path + "?" + u.query).status_code == 303
    c.cookies.clear()                                                         # dieselbe Antwort in einem anderen Browser
    r = c.get(u.path + "?" + u.query)
    assert r.status_code == 400 and c.get("/api/baum").status_code == 401
    c.cookies.set("stellwerk-anmeldung", offen)                               # und mit dem alten Cookie: der Code gilt nur einmal
    r = c.get(u.path + "?" + u.query)
    assert r.status_code == 400 and c.get("/api/baum").status_code == 401


def test_ablehnung_beim_identitaetsdienst(c, idp):
    idp.fehler = {"ablehnen": True}
    r = anmelden(c, idp)
    assert r.status_code == 400 and "abgelehnt" in r.text and "<b>" not in r.text.split("<main>")[1]


def test_ohne_rolle_kein_zugang(c, idp):
    r = anmelden(c, idp, ["pflege", "verwaltung"], name="f.fremd")
    assert r.status_code == 403 and "keiner Stellwerk-Rolle zugeordnet" in r.text and 'href="/anmelden?neu=1"' in r.text
    assert "prompt=login" in c.get("/anmelden", params={"neu": 1}).headers["location"]        # anderes Konto: neu abfragen
    assert "prompt=" not in c.get("/anmelden").headers["location"]
    assert c.get("/api/baum").status_code == 401
    assert [(e["wer"], e["aktion"]) for e in c.app.state.store.audit_log()] == [("f.fremd", "anmeldung.abgelehnt")]


def test_gruppen_aus_anderen_stellen(acfg, idp):
    from fastapi.testclient import TestClient

    from stellwerk.app import create_app
    acfg["anmeldung"]["oidc"]["rollen_claim"] = "realm_access.roles"            # Keycloak: Rollen des Realms
    with TestClient(create_app(acfg), base_url="http://localhost", follow_redirects=False) as cl:
        assert anmelden(cl, idp, None, realm_access={"roles": ["stellwerk-verwalten"]}).status_code == 303
        assert cl.get("/api/ich").json()["rollen"][-1] == "verwalten"
    acfg["anmeldung"]["oidc"]["rollen_claim"] = "http://schemas.xmlsoap.org/claims/Group"      # AD FS: Name mit Punkten
    with TestClient(create_app(acfg), base_url="http://localhost", follow_redirects=False) as cl:
        assert anmelden(cl, idp, None, **{"http://schemas.xmlsoap.org/claims/Group": "stellwerk-lesen"}).status_code == 303
        assert cl.get("/api/ich").json()["rollen"] == ["lesen"]


def test_gruppen_nur_in_den_nutzerangaben(c, idp):
    idp.nutzerinfo = {"sub": "u-m.muster", "groups": ["stellwerk-verwalten"]}
    assert anmelden(c, idp, None).status_code == 303 and c.get("/api/ich").json()["rollen"][-1] == "verwalten"
    idp.nutzerinfo = {"sub": "jemand-anderes", "groups": ["stellwerk-verwalten"]}      # Angaben zu einem anderen Nutzer zählen nicht
    assert anmelden(c, idp, None).status_code == 403


def test_sitzung_laeuft_ab(c, idp, monkeypatch):
    import time as zeit
    jetzt = [zeit.time()]
    monkeypatch.setattr(auth.time, "time", lambda: jetzt[0])
    anmelden(c, idp)
    jetzt[0] += 50 * 60
    assert c.get("/api/baum").status_code == 200                    # innerhalb der Stunde: bleibt angemeldet
    jetzt[0] += 50 * 60
    assert c.get("/api/baum").status_code == 200
    jetzt[0] += 61 * 60
    assert c.get("/api/baum").status_code == 401                    # eine Stunde nichts getan
    anmelden(c, idp)
    for _ in range(9):
        jetzt[0] += 55 * 60
        r = c.get("/api/baum")
    assert r.status_code == 401                                     # nach acht Stunden ist Schluss, auch bei Aktivität


def test_abmelden(c, idp):
    anmelden(c, idp, name="a.abel")
    token = c.cookies.get("stellwerk")
    r = c.post("/abmelden", headers=K)
    assert r.json()["weiter"].startswith(idp.url + "/logout?") and "post_logout_redirect_uri=http%3A%2F%2Flocalhost%2Fabgemeldet" in r.json()["weiter"]
    assert "id_token_hint=ey" in r.json()["weiter"]                  # damit der Identitätsdienst ohne Rückfrage abmeldet
    assert "Max-Age=0" in r.headers["set-cookie"]
    c.cookies.set("stellwerk", token)                                # auch mit dem alten Cookie geht nichts mehr
    assert c.get("/api/baum").status_code == 401
    assert c.app.state.store.audit_log()[0]["aktion"] == "abmeldung"
    assert c.get("/abgemeldet").status_code == 200
    c.cookies.clear()
    anmelden(c, idp)
    assert c.get("/abgemeldet").headers["location"] == "/" and c.get("/api/baum").status_code == 200     # ein Link dorthin meldet niemanden ab


def test_cookie_steht_nicht_in_der_datenbank(c, idp, acfg):
    anmelden(c, idp)
    token = c.cookies.get("stellwerk")
    assert len(token) >= 43
    con = sqlite3.connect(acfg["datenbank"][10:])
    zeilen = con.execute("SELECT id, art, name FROM sitzung").fetchall()
    alles = str(con.execute("SELECT * FROM sitzung").fetchall()) + str(con.execute("SELECT * FROM audit").fetchall())
    con.close()
    assert zeilen == [(hashlib.sha256(token.encode()).hexdigest(), "nutzer", "m.muster")]            # Anmeldeversuch ist gelöscht
    assert token not in alles and idp.client_secret not in alles


def test_ziel_nach_der_anmeldung_bleibt_in_der_anwendung(c, idp):
    assert anmelden(c, idp, ziel="/probieren").headers["location"] == "/probieren"
    for boese in ("https://fremd.example/", "//fremd.example", "/\\fremd.example", "javascript:alert(1)"):
        assert anmelden(c, idp, ziel=boese).headers["location"] == "/"


def test_dienst_token(c, bsp):
    hl7 = (bsp / "arztbrief_mdm.hl7").read_bytes()
    r = c.post("/v1/convert", content=hl7, headers={"Authorization": "Bearer " + TOKEN})
    assert r.status_code in (200, 422) and "dokumente" in r.json()
    assert c.get("/v1/health", headers={"Authorization": "Bearer " + TOKEN}).status_code == 200
    r = c.post("/v1/convert", content=hl7, headers={"Authorization": "Bearer " + "f" * 40})
    assert r.status_code == 401 and "Dienst-Token" in r.json()["fehler"]
    assert c.get("/api/baum", headers={"Authorization": "Bearer " + TOKEN}).status_code == 401      # nur für /v1
    assert c.get("/api/audit", headers={"Authorization": "Bearer " + TOKEN}).status_code == 401


def test_einstellungen_werden_geprueft(acfg, monkeypatch):
    def mit(**k):
        c = copy.deepcopy(acfg)
        for pfad, v in k.items():
            d = c["anmeldung"]
            *vor, letzter = pfad.split("__")
            for t in vor:
                d = d[t]
            d[letzter] = v
        return c
    assert auth.einstellungen(copy.deepcopy(config.DEFAULTS)) is None
    assert auth.einstellungen(acfg).antwort == "http://localhost/anmelden/antwort"
    assert auth.einstellungen(mit(oeffentliche_adresse="https://Stellwerk.Haus:443/")).adresse == "https://stellwerk.haus"
    assert auth.einstellungen(mit(oeffentliche_adresse="https://stellwerk.haus:8443")).adresse == "https://stellwerk.haus:8443"
    for falsch, meldung in [(mit(oidc__aussteller="http://login.haus"), "https"), (mit(oeffentliche_adresse="http://stellwerk.haus"), "https"),
                            (mit(oeffentliche_adresse="https://haus/stellwerk"), "keinen Pfad"), (mit(oidc__client_id=""), "fehlen Angaben"),
                            (mit(rollen={"lesen": [], "bearbeiten": [], "verwalten": []}), "niemand anmelden"),
                            (mit(rollen={"admin": ["x"]}), "unbekannt"), (mit(art="ldap"), "ldap"), (mit(sitzung_stunden=100), "sitzung_stunden")]:
        with pytest.raises(auth.AuthError, match=meldung):
            auth.einstellungen(falsch)
    monkeypatch.setenv("STELLWERK_DIENST_TOKEN", "kurz")
    with pytest.raises(auth.AuthError, match="zu kurz"):
        auth.einstellungen(acfg)


def test_merkmale_bei_https(acfg, idp):
    from stellwerk.db import Store
    acfg["anmeldung"]["oeffentliche_adresse"] = "https://stellwerk.haus"
    w = auth.Wache(acfg, Store(acfg["datenbank"]))
    assert w.s.cookie == "__Host-stellwerk"
    assert w.cookie(w.s.cookie, "wert", 60) == "__Host-stellwerk=wert; Path=/; Max-Age=60; HttpOnly; SameSite=Lax; Secure"
    from fastapi.testclient import TestClient

    from stellwerk.app import create_app
    with TestClient(create_app(acfg), base_url="https://stellwerk.haus", follow_redirects=False) as cl:
        r = cl.get("/healthz")
        assert r.headers["strict-transport-security"] == "max-age=31536000" and r.headers["x-frame-options"] == "DENY"
        assert cl.get("/api/baum").headers["content-security-policy"]              # auch Ablehnungen tragen die Schutz-Köpfe


def test_aussteller_muss_stimmen(c, idp):
    idp.fehler = {"aussteller_in_beschreibung": "http://127.0.0.1:1"}
    r = c.get("/anmelden")
    assert r.status_code == 502 and "nennt sich" in r.text


def test_identitaetsdienst_nicht_erreichbar(acfg, idp):
    from fastapi.testclient import TestClient

    from stellwerk.app import create_app
    idp.stop()
    with TestClient(create_app(acfg), base_url="http://localhost", follow_redirects=False) as cl:
        r = cl.get("/anmelden")
        assert r.status_code == 502 and "nicht erreichbar" in r.text
        assert cl.get("/healthz").status_code == 200


def test_rs256(schluessel):
    (n, e, d), (n2, _, _) = schluessel
    daten = b"kopf.inhalt"
    sig = rs256(daten, n, d)
    assert auth.rs256_ok(daten, sig, n, e)
    assert not auth.rs256_ok(b"kopf.inhalT", sig, n, e)
    assert not auth.rs256_ok(daten, sig, n2, e)
    assert not auth.rs256_ok(daten, sig[:-1], n, e) and not auth.rs256_ok(daten, b"\x00" + sig, n, e)
    assert not auth.rs256_ok(daten, bytes(len(sig)), n, e)
    assert not auth.rs256_ok(daten, (n + 1).to_bytes(len(sig), "big"), n, e)
    kurz = 0xC5F7B5B5E9B7C1D3 * 0xE3A1F9D7B5C3A1F7                                # 128 Bit: zu kurze Schlüssel gelten nie
    assert not auth.rs256_ok(daten, b"\x01" * 16, kurz, 3)


def test_rs256_gegen_bibliothek(schluessel):
    """Dieselbe Prüfung wie die Bibliothek cryptography, in beiden Richtungen."""
    pytest.importorskip("cryptography")
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key().public_numbers()
    for i in range(20):
        daten = f"kopf{i}.inhalt{i}".encode()
        sig = key.sign(daten, padding.PKCS1v15(), hashes.SHA256())
        assert auth.rs256_ok(daten, sig, pub.n, pub.e)
        kaputt = bytearray(sig)
        kaputt[i * 7 % len(sig)] ^= 1 << (i % 8)
        assert not auth.rs256_ok(daten, bytes(kaputt), pub.n, pub.e)
    (n, e, d), _ = schluessel                                                      # und umgekehrt: unsere Testsignatur
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
    RSAPublicNumbers(e, n).public_key().verify(rs256(b"a.b", n, d), b"a.b", padding.PKCS1v15(), hashes.SHA256())
    with pytest.raises(InvalidSignature):
        RSAPublicNumbers(e, n).public_key().verify(rs256(b"a.b", n, d), b"a.c", padding.PKCS1v15(), hashes.SHA256())


def test_ohne_anmeldung_bleibt_alles_wie_bisher(client):
    assert client.get("/api/ich").json() == {"anmeldung": "keine", "angemeldet": True, "name": "lokal", "rollen": ["lesen", "bearbeiten", "verwalten"]}
    assert client.put("/api/baum", json={"kinder": []}).status_code == 200
    assert client.get("/anmelden", follow_redirects=False).headers["location"] == "/"


def test_datenbank_nur_auf_freigegebenen_rechnern(c, idp, acfg, monkeypatch):
    """Wer nicht verwaltet, darf den Dienst nicht zu einem beliebigen Rechner verbinden lassen (Passwort, Sprungbrett)."""
    monkeypatch.setenv("STELLWERK_DB_KIS", "geheim")
    fremd = {"art": "sql", "db": "postgresql://kis:${STELLWERK_DB_KIS}@fremd.example/kis", "abfrage": "SELECT 1"}
    nah = {"art": "sql", "db": "postgresql://kis:${STELLWERK_DB_KIS}@127.0.0.1:1/kis", "abfrage": "SELECT 1"}
    anmelden(c, idp, ["stellwerk-bearbeiten"])
    c.put("/api/baum", json={"kinder": [{"id": "task000001", "typ": "aufgabe", "name": "A"}]}, headers=K)
    r = c.put("/api/aufgabe/task000001", json=fremd, headers=K)
    assert r.status_code == 403 and "fremd.example ist nicht freigegeben" in r.json()["fehler"]
    assert c.post("/api/erschliessen", json={"db": fremd["db"]}, headers=K).status_code == 403
    assert c.put("/api/aufgabe/task000001", json={"art": "datei", "db": fremd["db"]}, headers=K).status_code == 200     # ungenutzt: egal
    assert c.put("/api/aufgabe/task000001", json=fremd, headers=K).status_code == 403                                     # sobald sie gilt: geprüft
    assert c.put("/api/aufgabe/task000001", json={"art": "sql", "db": "sqlite:////daten/kis.db", "abfrage": "SELECT 1"}, headers=K).status_code == 200

    anmelden(c, idp, ["stellwerk-verwalten"])
    assert c.put("/api/aufgabe/task000001", json=nah, headers=K).status_code == 200
    anmelden(c, idp, ["stellwerk-bearbeiten"])
    assert c.put("/api/aufgabe/task000001", json={**nah, "abfrage": "SELECT 2"}, headers=K).status_code == 200            # Adresse unverändert
    r = c.post("/api/erschliessen", json={"db": nah["db"], "aufgabe": "task000001"}, headers=K)
    assert r.status_code not in (401, 403)                                                                                # geprüft, scheitert erst an der Verbindung
    assert c.post("/api/erschliessen", json={"db": nah["db"], "aufgabe": "task999999"}, headers=K).status_code == 403
    assert c.put("/api/aufgabe/task000001", json=fremd, headers=K).status_code == 403


def test_freigabeliste_gilt_fuer_alle(acfg, idp, monkeypatch):
    from fastapi.testclient import TestClient

    from stellwerk.app import create_app
    monkeypatch.setenv("STELLWERK_DB_KIS", "geheim")
    acfg["quellen"] = {"erlaubte_hosts": ["kis-db.haus", "LIS-DB.haus:1521"]}
    def sql(db):
        return {"art": "sql", "db": db, "abfrage": "SELECT 1"}
    with TestClient(create_app(acfg), base_url="http://localhost", follow_redirects=False) as cl:
        anmelden(cl, idp, ["stellwerk-bearbeiten"])
        cl.put("/api/baum", json={"kinder": [{"id": "task000001", "typ": "aufgabe", "name": "A"}]}, headers=K)
        put = lambda db: cl.put("/api/aufgabe/task000001", json=sql(db), headers=K)       # noqa: E731
        assert put("postgresql://kis:${STELLWERK_DB_KIS}@kis-db.haus:5432/kis").status_code == 200
        assert put("oracle+oracledb://lis:${STELLWERK_DB_KIS}@lis-db.haus:1521/?service_name=LIS").status_code == 200
        assert put("oracle+oracledb://lis:${STELLWERK_DB_KIS}@lis-db.haus:1522/?service_name=LIS").status_code == 403    # anderer Port
        assert "sslmode" in put("postgresql://kis:${STELLWERK_DB_KIS}@kis-db.haus/kis?sslmode=disable").json()["fehler"]
        anmelden(cl, idp, ["stellwerk-verwalten"])
        assert put("postgresql://kis:${STELLWERK_DB_KIS}@kis-db.haus/kis?sslmode=require").status_code == 200
        assert put("postgresql://kis:${STELLWERK_DB_KIS}@fremd.example/kis").status_code == 403                          # auch Verwalter nicht
        r = put("postgresql://kis:${STELLWERK_DB_KIS}@kis-db.haus/kis?host=fremd.example")
        assert r.status_code == 403 and "Ziel der Verbindung" in r.json()["fehler"]
        assert put("mssql+pyodbc:///?odbc_connect=SERVER%3Dfremd.example").status_code == 403


def test_ziel_einer_datenbankadresse(monkeypatch):
    from stellwerk.sqlsource import connection_target
    monkeypatch.setenv("STELLWERK_DB_X", "p@ss/wort")
    monkeypatch.setenv("STELLWERK_DB_HOST", "db.haus")
    assert connection_target("sqlite:////daten/kis.db") == ("", "", [], [])
    assert connection_target("postgresql+psycopg://u:${STELLWERK_DB_X}@DB.Haus:5432/kis") == ("db.haus", "5432", [], [])
    assert connection_target("postgresql://u:${STELLWERK_DB_X}@${STELLWERK_DB_HOST}/kis?Host=b&sslmode=x&charset=utf8") == ("db.haus", "", ["host"], ["sslmode"])
    assert connection_target("mssql+pyodbc:///?odbc_connect=x&driver=y") == ("", "", ["odbc_connect"], [])


def test_start_ohne_anmeldung_nur_lokal(tmp_path, monkeypatch, capsys):
    from stellwerk import cli
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STELLWERK_OHNE_ANMELDUNG", raising=False)
    assert cli.main(["serve", "--host", "0.0.0.0", "--port", "1"]) == 2
    assert "nur auf 127.0.0.1" in capsys.readouterr().err


def test_befehl_anmeldung(acfg, idp, tmp_path, monkeypatch, capsys):
    import yaml

    from stellwerk import cli
    from stellwerk.db import Store
    monkeypatch.chdir(tmp_path)
    datei = tmp_path / "stellwerk.yaml"
    datei.write_text(yaml.safe_dump({k: acfg[k] for k in ("anmeldung", "datenbank")}), encoding="utf-8")
    assert cli.main(["--config", str(datei), "anmeldung"]) == 0
    aus = capsys.readouterr().out
    assert "http://localhost/anmelden/antwort" in aus and "http://localhost/abgemeldet" in aus and "mit Geheimnis" in aus
    assert idp.client_secret not in aus and TOKEN not in aus

    st = Store(acfg["datenbank"])
    st.migrate()
    import time as zeit
    jetzt = int(zeit.time())
    st.sitzung_anlegen("a" * 64, "nutzer", laeuft_ab=jetzt + 3600, jetzt=jetzt, nutzer="u1", name="m.muster", rollen=["lesen", "bearbeiten"])
    st.sitzung_anlegen("b" * 64, "nutzer", laeuft_ab=jetzt + 3600, jetzt=jetzt, nutzer="u2", name="v.vogt", rollen=["lesen", "bearbeiten", "verwalten"])
    assert cli.main(["--config", str(datei), "anmeldung", "--sitzungen"]) == 0
    aus = capsys.readouterr().out
    assert "2 laufende" in aus and "m.muster" in aus and "verwalten" in aus and "a" * 64 not in aus
    assert cli.main(["--config", str(datei), "anmeldung", "--beenden", "m.muster"]) == 0
    assert st.sitzung("a" * 64) is None and st.sitzung("b" * 64) is not None
    assert cli.main(["--config", str(datei), "anmeldung", "--beenden", "alle"]) == 0 and st.sitzungen(0) == []

    idp.fehler = {"aussteller_in_beschreibung": "http://127.0.0.1:1"}
    assert cli.main(["--config", str(datei), "anmeldung"]) == 2 and "nennt sich" in capsys.readouterr().err
