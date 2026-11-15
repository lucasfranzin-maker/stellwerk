"""Anmeldung über OpenID Connect (Keycloak, AD FS, Entra ID), Rollen und Sitzungen.

Stellwerk speichert keine Passwörter. Nutzer melden sich beim Identitätsdienst des Hauses an; von dort kommen Kennung,
Name und Gruppen. Gruppen werden auf drei Rollen abgebildet (lesen, bearbeiten, verwalten), die Sitzung führt Stellwerk
selbst: ein zufälliger Wert im Cookie, in der Datenbank nur dessen SHA-256.

Ablauf: Authorization Code mit PKCE. Das ID-Token wird vollständig geprüft: RS256-Signatur gegen die Schlüssel des
Ausstellers, Aussteller, Empfänger, Ablauf, Nonce. Andere Signaturverfahren werden abgelehnt.

Maschinen (Kommunikationsserver) nutzen /v1/... mit einem Dienst-Token aus einer Umgebungsvariable.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

ROLLEN = ("lesen", "bearbeiten", "verwalten")       # aufsteigend: jede Rolle schließt die davor ein
OFFEN = "offen"
SICHER = ("GET", "HEAD")
KOPF_ANFRAGE = "x-stellwerk-anfrage"                # eigener Kopf an jeder ändernden Anfrage der Oberfläche (Schutz vor CSRF)
MIN_TOKEN = 32
LOKAL = ("127.0.0.1", "localhost", "::1", "[::1]")

# Wer darf was. Reihenfolge egal, erster Treffer gilt; was hier nicht steht, braucht „verwalten“.
REGELN: list[tuple[str, str, str]] = [
    ("GET", r"/(healthz|readyz)", OFFEN),
    ("GET", r"/fonts/[^/]+", OFFEN),
    ("GET", r"/(anmelden|anmelden/antwort|abgemeldet)", OFFEN),
    ("POST", r"/abmelden", OFFEN),
    ("GET", r"/api/ich", OFFEN),
    ("GET", r"/", "lesen"),
    ("GET", r"/api/(baum|laeufe|zustellung|bridgelink/status|openapi\.json)", "lesen"),
    ("GET", r"/api/aufgabe/[^/]+(/(bundle|kanal))?", "lesen"),
    ("GET", r"/v1/health", "lesen"),
    ("GET", r"/probieren", "bearbeiten"),
    ("PUT", r"/api/baum", "bearbeiten"),
    ("PUT", r"/api/aufgabe/[^/]+", "bearbeiten"),
    ("POST", r"/api/aufgabe/[^/]+/(quelle|lauf|ablegen)", "bearbeiten"),
    ("GET", r"/api/aufgabe/[^/]+/uebergabe", "bearbeiten"),      # mit echten Zeilen: „verwalten“, prüft der Handler
    ("POST", r"/api/erschliessen", "bearbeiten"),
    ("POST", r"/v1/convert", "bearbeiten"),
    ("POST", r"/api/aufgabe/[^/]+/(senden|kanal)", "verwalten"),
    ("GET", r"/api/audit", "verwalten"),
]
_REGELN = [(m, re.compile(p + r"\Z"), r) for m, p, r in REGELN]


class AuthError(Exception):
    """Die Meldung ist für Menschen geschrieben und enthält keine Geheimnisse."""


def rolle_fuer(method: str, path: str) -> str:
    method = "GET" if method == "HEAD" else method
    for m, rx, rolle in _REGELN:
        if m == method and rx.match(path):
            return rolle
    return "verwalten"


def hat(rollen, noetig: str) -> bool:
    if noetig == OFFEN:
        return True
    have = max((ROLLEN.index(r) for r in rollen or () if r in ROLLEN), default=-1)
    return have >= ROLLEN.index(noetig)


def _alle_bis(rolle: str) -> list[str]:
    return list(ROLLEN[:ROLLEN.index(rolle) + 1])


@dataclass
class Ich:
    """Wer eine Anfrage stellt."""
    name: str
    rollen: list[str]
    art: str = "nutzer"                 # nutzer | dienst | lokal
    sub: str = ""
    sitzung: str = ""                   # Kennung der Sitzung in der Datenbank (nicht der Cookie-Wert)


LOKAL_ICH = Ich(name="lokal", rollen=list(ROLLEN), art="lokal")


# --------------------------------------------------------------------------
# Einstellungen
# --------------------------------------------------------------------------

@dataclass
class Einstellungen:
    aussteller: str
    client_id: str
    client_secret: str
    adresse: str                         # öffentliche Adresse von Stellwerk, ohne Schrägstrich am Ende
    scopes: str = "openid profile email"
    ca: str = ""
    proxy: str = ""
    rollen_claim: str = "groups"
    name_claim: str = "preferred_username"
    rollen: dict = field(default_factory=dict)
    sitzung_s: int = 8 * 3600
    leerlauf_s: int = 3600
    dienst_token: str = ""

    @property
    def https(self) -> bool:
        return self.adresse.startswith("https://")

    @property
    def cookie(self) -> str:
        return "__Host-stellwerk" if self.https else "stellwerk"

    @property
    def cookie_offen(self) -> str:
        return self.cookie + "-anmeldung"

    @property
    def antwort(self) -> str:
        return self.adresse + "/anmelden/antwort"


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def _nur_https(url: str, was: str) -> None:
    u = urllib.parse.urlsplit(url)
    if u.scheme == "https" and u.hostname:
        return
    if u.scheme == "http" and (u.hostname or "").lower() in LOKAL:      # nur zum Ausprobieren auf dem eigenen Rechner
        return
    raise AuthError(f"{was} muss mit https:// beginnen (http nur für localhost): {url or '(leer)'}")


def einstellungen(cfg: dict) -> Einstellungen | None:
    """Einstellungen prüfen. None heißt: ohne Anmeldung (nur für den eigenen Rechner)."""
    a = cfg.get("anmeldung") or {}
    art = a.get("art") or "keine"
    if art == "keine":
        return None
    if art != "oidc":
        raise AuthError(f"anmeldung.art kennt nur „keine“ und „oidc“, nicht „{art}“.")
    o = a.get("oidc") or {}
    aussteller = str(o.get("aussteller") or "").rstrip("/")
    adresse = str(a.get("oeffentliche_adresse") or "").rstrip("/")
    if not aussteller or not o.get("client_id") or not adresse:
        raise AuthError("Für die Anmeldung fehlen Angaben: anmeldung.oidc.aussteller, anmeldung.oidc.client_id und "
                        "anmeldung.oeffentliche_adresse (die Adresse, unter der Nutzer Stellwerk aufrufen).")
    _nur_https(aussteller, "anmeldung.oidc.aussteller")
    _nur_https(adresse, "anmeldung.oeffentliche_adresse")
    u = urllib.parse.urlsplit(adresse)
    if u.path not in ("", "/") or u.query or u.username:
        raise AuthError("anmeldung.oeffentliche_adresse darf keinen Pfad enthalten (nur https://name[:port]).")
    try:
        port = u.port
    except ValueError as exc:
        raise AuthError("anmeldung.oeffentliche_adresse hat keinen gültigen Port.") from exc
    host = u.hostname if ":" not in u.hostname else f"[{u.hostname}]"
    adresse = f"{u.scheme}://{host}" + (f":{port}" if port and port != {"https": 443, "http": 80}[u.scheme] else "")   # so schreibt es der Browser
    rollen = {r: [str(g).strip() for g in ((a.get("rollen") or {}).get(r) or []) if str(g).strip()] for r in ROLLEN}
    unbekannt = sorted(set(a.get("rollen") or {}) - set(ROLLEN))
    if unbekannt:
        raise AuthError(f"anmeldung.rollen kennt nur {', '.join(ROLLEN)}; unbekannt: {', '.join(unbekannt)}.")
    if not any(rollen.values()):
        raise AuthError("anmeldung.rollen ist leer. Mindestens einer Rolle muss eine Gruppe des Identitätsdienstes "
                        "zugeordnet sein, sonst kann sich niemand anmelden.")
    token = os.environ.get(a.get("dienst_token_env") or "STELLWERK_DIENST_TOKEN", "")
    if token and len(token) < MIN_TOKEN:
        raise AuthError(f"Das Dienst-Token ist zu kurz (mindestens {MIN_TOKEN} Zeichen). "
                        "Erzeugen mit: python -c \"import secrets; print(secrets.token_urlsafe(32))\"")
    stunden, leerlauf = a.get("sitzung_stunden", 8), a.get("leerlauf_minuten", 60)
    if not (isinstance(stunden, (int, float)) and 0 < stunden <= 24 and isinstance(leerlauf, (int, float)) and 0 < leerlauf <= 24 * 60):
        raise AuthError("anmeldung.sitzung_stunden muss zwischen 0 und 24 liegen, anmeldung.leerlauf_minuten zwischen 0 und 1440.")
    return Einstellungen(
        aussteller=aussteller, client_id=str(o["client_id"]),
        client_secret=os.environ.get(o.get("client_secret_env") or "STELLWERK_OIDC_SECRET", ""),
        adresse=adresse, scopes=str(o.get("scopes") or "openid profile email"), ca=str(o.get("ca") or ""),
        proxy=str(o.get("proxy") or ""), rollen_claim=str(o.get("rollen_claim") or "groups"),
        name_claim=str(o.get("name_claim") or "preferred_username"), rollen=rollen,
        sitzung_s=int(stunden * 3600), leerlauf_s=int(leerlauf * 60), dienst_token=token)


# --------------------------------------------------------------------------
# ID-Token prüfen
# --------------------------------------------------------------------------

def _b64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")     # DigestInfo für SHA-256 (RFC 8017, 9.2)


def rs256_ok(signiert: bytes, signatur: bytes, n: int, e: int) -> bool:
    """RSASSA-PKCS1-v1_5 mit SHA-256 prüfen.

    Der erwartete Block wird vollständig aufgebaut und als Ganzes verglichen. Es wird nichts aus der Signatur
    herausgelesen, deshalb greifen die bekannten Fälschungen über nachlässiges Parsen hier nicht.
    """
    k = (n.bit_length() + 7) // 8
    if n.bit_length() < 2048 or e < 3 or e % 2 == 0 or len(signatur) != k:
        return False
    s = int.from_bytes(signatur, "big")
    if s >= n:
        return False
    t = _SHA256_PREFIX + hashlib.sha256(signiert).digest()
    erwartet = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(pow(s, e, n).to_bytes(k, "big"), erwartet)


def _claim(claims: dict, pfad: str):
    """Claim lesen. Ein Name mit Punkt ist erst der ganze Schlüssel (AD FS nutzt URLs), sonst ein Pfad (realm_access.roles)."""
    if pfad in claims:
        return claims[pfad]
    cur = claims
    for teil in pfad.split("."):
        if not isinstance(cur, dict) or teil not in cur:
            return None
        cur = cur[teil]
    return cur


def gruppen(claims: dict, pfad: str) -> list[str] | None:
    v = _claim(claims, pfad)
    if v is None:
        return None
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [str(x) for x in v if isinstance(x, (str, int))]
    return []


def rollen_aus(gr: list[str], abbildung: dict) -> list[str]:
    """Höchste Rolle, deren Gruppen der Nutzer hat. Vergleich ohne Groß-/Kleinschreibung, sonst genau."""
    have = {g.casefold() for g in gr}
    for rolle in reversed(ROLLEN):
        if any(g.casefold() in have for g in abbildung.get(rolle) or []):
            return _alle_bis(rolle)
    return []


class _KeinRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Oidc:
    """Gespräch mit dem Identitätsdienst. Ohne Systemproxy, ohne Weiterleitungen, Zertifikate werden geprüft."""

    def __init__(self, s: Einstellungen, timeout: float = 10):
        self.s = s
        self.timeout = timeout
        ctx = ssl.create_default_context(cafile=s.ca or None)
        self._open = urllib.request.build_opener(
            urllib.request.ProxyHandler({"https": s.proxy, "http": s.proxy} if s.proxy else {}),
            urllib.request.HTTPSHandler(context=ctx), _KeinRedirect).open
        self._meta: dict | None = None
        self._meta_zeit = 0.0
        self._keys: dict[str, tuple[int, int]] = {}
        self._keys_zeit = 0.0

    def _json(self, url: str, data: dict | None = None, headers: dict | None = None, was: str = "") -> dict:
        _nur_https(url, was or "Adresse des Identitätsdienstes")
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, headers={"Accept": "application/json", **(headers or {})})
        try:
            with self._open(req, timeout=self.timeout) as r:
                raw = r.read(1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                err = json.loads(exc.read(4096).decode("utf-8", "replace"))
                detail = str(err.get("error_description") or err.get("error") or "")[:200]
            except (ValueError, OSError):
                pass
            raise AuthError(f"Der Identitätsdienst antwortet mit {exc.code}{' (' + detail + ')' if detail else ''} ({was}).") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise AuthError(f"Der Identitätsdienst ist nicht erreichbar ({was}): {getattr(exc, 'reason', exc)}") from exc
        if len(raw) > 1024 * 1024:
            raise AuthError(f"Antwort des Identitätsdienstes ist zu groß ({was}).")
        try:
            val = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise AuthError(f"Antwort des Identitätsdienstes ist kein JSON ({was}).") from exc
        if not isinstance(val, dict):
            raise AuthError(f"Antwort des Identitätsdienstes hat nicht die erwartete Form ({was}).")
        return val

    def meta(self) -> dict:
        if self._meta and time.time() - self._meta_zeit < 3600:
            return self._meta
        m = self._json(self.s.aussteller + "/.well-known/openid-configuration", was="Beschreibung des Ausstellers")
        if str(m.get("issuer") or "").rstrip("/") != self.s.aussteller:
            raise AuthError(f"Der Identitätsdienst nennt sich „{m.get('issuer')}“, eingetragen ist „{self.s.aussteller}“. "
                            "anmeldung.oidc.aussteller muss genau dem Aussteller entsprechen.")
        for k in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not m.get(k):
                raise AuthError(f"In der Beschreibung des Ausstellers fehlt {k}.")
            _nur_https(m[k], k)
        self._meta, self._meta_zeit = m, time.time()
        return m

    def anmelde_adresse(self, state: str, nonce: str, challenge: str, neu: bool = False) -> str:
        q = {"response_type": "code", "client_id": self.s.client_id, "redirect_uri": self.s.antwort, "scope": self.s.scopes,
             "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
        if neu:                                         # anderes Konto: der Identitätsdienst soll die Anmeldung neu abfragen
            q["prompt"] = "login"
        ep = self.meta()["authorization_endpoint"]
        return ep + ("&" if "?" in ep else "?") + urllib.parse.urlencode(q)

    def abmelde_adresse(self, id_token: str = "") -> str:
        """Adresse, die auch die Sitzung beim Identitätsdienst beendet. Mit dem ID-Token als Hinweis fragt er nicht nach."""
        try:
            ep = self.meta().get("end_session_endpoint")
        except AuthError:
            ep = ""
        if not ep:
            return ""
        q = {"client_id": self.s.client_id, "post_logout_redirect_uri": self.s.adresse + "/abgemeldet"}
        if id_token and len(id_token) <= 6000:                  # sehr lange Token sprengen die Adresszeile
            q["id_token_hint"] = id_token
        return ep + ("&" if "?" in ep else "?") + urllib.parse.urlencode(q)

    def tauschen(self, code: str, verifier: str) -> dict:
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.s.antwort,
                "client_id": self.s.client_id, "code_verifier": verifier}
        headers = {}
        if self.s.client_secret:                       # vertraulicher Client: Basic nach RFC 6749, 2.3.1
            pair = f"{urllib.parse.quote(self.s.client_id, safe='')}:{urllib.parse.quote(self.s.client_secret, safe='')}"
            headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
        tok = self._json(self.meta()["token_endpoint"], data, headers, was="Einlösen des Anmeldecodes")
        if not isinstance(tok.get("id_token"), str):
            raise AuthError("Der Identitätsdienst hat kein ID-Token geliefert. Im Client muss der Scope „openid“ erlaubt sein.")
        return tok

    def _schluessel(self, kid: str) -> tuple[int, int] | None:
        if kid not in self._keys and time.time() - self._keys_zeit > 30:        # unbekannt: neu holen, höchstens alle 30 s
            self._keys_zeit = time.time()
            keys = {}
            for k in self._json(self.meta()["jwks_uri"], was="Schlüssel des Ausstellers").get("keys") or []:
                if isinstance(k, dict) and k.get("kty") == "RSA" and k.get("use", "sig") == "sig" and k.get("n") and k.get("e"):
                    try:
                        keys[str(k.get("kid") or "")] = (int.from_bytes(_b64(k["n"]), "big"), int.from_bytes(_b64(k["e"]), "big"))
                    except (ValueError, TypeError):
                        continue
            self._keys = keys
        if not kid and len(self._keys) == 1:                                    # manche Dienste nennen bei einem Schlüssel keine Kennung
            return next(iter(self._keys.values()))
        return self._keys.get(kid)

    def pruefen(self, id_token: str, nonce: str) -> dict:
        """ID-Token prüfen und die Claims liefern. Jede Abweichung ist ein AuthError."""
        teile = id_token.split(".")
        if len(teile) != 3:
            raise AuthError("Das ID-Token hat nicht die erwartete Form.")
        try:
            kopf, claims, sig = json.loads(_b64(teile[0])), json.loads(_b64(teile[1])), _b64(teile[2])
        except (ValueError, TypeError) as exc:
            raise AuthError("Das ID-Token ist nicht lesbar.") from exc
        if not isinstance(kopf, dict) or not isinstance(claims, dict):
            raise AuthError("Das ID-Token hat nicht die erwartete Form.")
        if kopf.get("alg") != "RS256":
            raise AuthError(f"Das ID-Token ist mit „{kopf.get('alg')}“ signiert. Stellwerk prüft RS256; "
                            "im Identitätsdienst für diesen Client RS256 einstellen.")
        key = self._schluessel(str(kopf.get("kid") or ""))
        if not key or not rs256_ok(f"{teile[0]}.{teile[1]}".encode("ascii"), sig, *key):
            raise AuthError("Die Signatur des ID-Tokens stimmt nicht mit den Schlüsseln des Ausstellers überein.")
        now = time.time()
        if str(claims.get("iss") or "").rstrip("/") != self.s.aussteller:
            raise AuthError("Das ID-Token stammt von einem anderen Aussteller.")
        aud = claims.get("aud")
        aud = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
        if self.s.client_id not in aud or (len(aud) > 1 and claims.get("azp") != self.s.client_id):
            raise AuthError("Das ID-Token ist nicht für Stellwerk ausgestellt.")
        if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] < now - 60:
            raise AuthError("Das ID-Token ist abgelaufen. Stimmt die Uhrzeit auf dem Stellwerk-Server?")
        if isinstance(claims.get("iat"), (int, float)) and claims["iat"] > now + 300:
            raise AuthError("Das ID-Token ist in der Zukunft ausgestellt. Stimmt die Uhrzeit auf dem Stellwerk-Server?")
        if not nonce or not hmac.compare_digest(str(claims.get("nonce") or "").encode(), nonce.encode()):
            raise AuthError("Das ID-Token gehört nicht zu dieser Anmeldung.")
        if not claims.get("sub"):
            raise AuthError("Das ID-Token nennt keinen Nutzer.")
        return claims

    def nutzerinfo(self, access_token: str, sub: str) -> dict:
        ep = self.meta().get("userinfo_endpoint")
        if not ep or not access_token:
            return {}
        info = self._json(ep, headers={"Authorization": "Bearer " + access_token}, was="Nutzerangaben")
        return info if info.get("sub") == sub else {}          # nur Angaben zum selben Nutzer übernehmen


# --------------------------------------------------------------------------
# Wache: Anfragen einordnen, Anmeldung führen, Sitzungen halten
# --------------------------------------------------------------------------

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", "replace")).hexdigest()


def _ziel(z: str | None) -> str:
    """Nur Pfade dieser Anwendung, nie eine fremde Adresse."""
    z = z or "/"
    return z if re.fullmatch(r"/[A-Za-z0-9_\-./?=&%]*", z) and not z.startswith("//") else "/"


class Abgelehnt(Exception):
    def __init__(self, code: int, meldung: str):
        super().__init__(meldung)
        self.code = code


class Wache:
    def __init__(self, cfg: dict, store, oidc: Oidc | None = None):
        self.s = einstellungen(cfg)
        self.store = store
        self.oidc = oidc or (Oidc(self.s) if self.s else None)

    @property
    def aktiv(self) -> bool:
        return self.s is not None

    # --- Anfrage einordnen
    def wer(self, method: str, path: str, cookies: dict, headers) -> Ich | None:
        if not self.s:
            return LOKAL_ICH
        auth = headers.get("authorization") or ""
        if path.startswith("/v1/") and auth[:7].lower() == "bearer ":
            if self.s.dienst_token and hmac.compare_digest(auth[7:].strip().encode(), self.s.dienst_token.encode()):
                return Ich(name="dienst", rollen=_alle_bis("bearbeiten"), art="dienst")
            raise Abgelehnt(401, "Das Dienst-Token ist nicht gültig.")
        token = cookies.get(self.s.cookie)
        if not token:
            return None
        now = int(time.time())
        row = self.store.sitzung(_hash(token))
        if not row or row["art"] != "nutzer" or row["laeuft_ab"] < now or row["zuletzt"] + self.s.leerlauf_s < now:
            return None
        if now - row["zuletzt"] > 60:
            self.store.sitzung_beruehren(row["id"], now)
        return Ich(name=row["name"], rollen=list(row["rollen"]), sub=row["nutzer"], sitzung=row["id"])

    def pruefen(self, method: str, path: str, cookies: dict, headers) -> Ich:
        """Darf diese Anfrage durch? Liefert, wer fragt, oder wirft Abgelehnt (401 nicht angemeldet, 403 nicht erlaubt)."""
        noetig = rolle_fuer(method, path)
        ich = self.wer(method, path, cookies, headers)
        if noetig == OFFEN:
            return ich or Ich(name="", rollen=[], art="niemand")
        if ich is None:
            raise Abgelehnt(401, "Nicht angemeldet.")
        if not hat(ich.rollen, noetig):
            raise Abgelehnt(403, f"Dafür braucht es die Rolle „{noetig}“. Sie haben: {', '.join(ich.rollen) or 'keine'}.")
        if ich.art == "nutzer" and method not in SICHER:
            self._csrf(headers)
        return ich

    def _csrf(self, headers) -> None:
        """Ändernde Anfragen mit Cookie müssen von der eigenen Oberfläche kommen."""
        origin, site = headers.get("origin"), headers.get("sec-fetch-site")
        if (origin and origin.rstrip("/") != self.s.adresse) or (site and site not in ("same-origin", "none")) \
                or not headers.get(KOPF_ANFRAGE):
            raise Abgelehnt(403, "Die Anfrage kommt nicht aus der Stellwerk-Oberfläche.")

    # --- Anmeldung
    def beginnen(self, ziel: str | None, neu: bool = False) -> tuple[str, str]:
        """Anmeldung starten. Liefert die Adresse beim Identitätsdienst und den Wert für das Anmelde-Cookie.

        State, Nonce und PKCE-Wert liegen bis zur Antwort nur im Cookie des Browsers (HttpOnly). So legt ein Besucher,
        der sich noch nicht angemeldet hat, nichts in der Datenbank an."""
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        url = self.oidc.anmelde_adresse(state, nonce, challenge, neu)
        offen = {"s": state, "n": nonce, "v": verifier, "z": _ziel(ziel), "t": int(time.time())}
        return url, base64.urlsafe_b64encode(json.dumps(offen, separators=(",", ":")).encode()).rstrip(b"=").decode()

    def abschliessen(self, offen_token: str | None, query: dict) -> tuple[str, str, Ich]:
        """Antwort des Identitätsdienstes verarbeiten. Liefert Sitzungs-Cookie, Ziel und den Nutzer."""
        try:
            o = json.loads(_b64(offen_token or ""))
            d = {"state": str(o["s"]), "nonce": str(o["n"]), "verifier": str(o["v"]), "ziel": _ziel(str(o["z"]))}
            alt = time.time() - float(o["t"]) > 600
        except (ValueError, TypeError, KeyError):
            d, alt = None, True
        if alt or not d["state"]:
            raise AuthError("Diese Anmeldung ist abgelaufen oder wurde in einem anderen Fenster begonnen. Bitte neu anmelden.")
        if not hmac.compare_digest(str(query.get("state") or "").encode(), d["state"].encode()):
            raise AuthError("Die Antwort des Identitätsdienstes gehört nicht zu dieser Anmeldung. Bitte neu anmelden.")
        if query.get("error"):
            was = re.sub(r"[^A-Za-z0-9_ .,:\-äöüÄÖÜß]", "", str(query.get("error_description") or query["error"]))[:200]
            raise AuthError(f"Der Identitätsdienst hat die Anmeldung abgelehnt: {was}")
        if not query.get("code"):
            raise AuthError("Der Identitätsdienst hat keinen Anmeldecode geliefert.")
        tok = self.oidc.tauschen(str(query["code"]), d["verifier"])
        claims = self.oidc.pruefen(tok["id_token"], d["nonce"])
        gr = gruppen(claims, self.s.rollen_claim)
        if gr is None:                                                        # manche Dienste liefern Gruppen nur hier
            info = self.oidc.nutzerinfo(str(tok.get("access_token") or ""), claims["sub"])
            gr = gruppen(info, self.s.rollen_claim) or []
            claims = {**info, **claims}
        name = str(claims.get(self.s.name_claim) or claims.get("email") or claims["sub"])[:200]
        rollen = rollen_aus(gr, self.s.rollen)
        if not rollen:
            self.store.audit(name, "anmeldung.abgelehnt", detail="keiner Rolle zugeordnet")
            raise Abgelehnt(403, f"Sie sind als „{name}“ angemeldet, aber keiner Stellwerk-Rolle zugeordnet. Wer Stellwerk betreut, "
                                 "kann Ihre Gruppe einer Rolle zuordnen (anmeldung.rollen in der Konfiguration).")
        token, now = secrets.token_urlsafe(32), int(time.time())
        sid = _hash(token)
        self.store.sitzungen_aufraeumen(now)
        self.store.sitzung_anlegen(sid, "nutzer", laeuft_ab=now + self.s.sitzung_s, jetzt=now,
                                   nutzer=str(claims["sub"])[:200], name=name, rollen=rollen,
                                   daten={"id_token": tok["id_token"]})          # nur als Hinweis beim Abmelden
        self.store.audit(name, "anmeldung.erfolgreich", detail="Rolle " + rollen[-1])
        return token, d["ziel"], Ich(name=name, rollen=rollen, sub=str(claims["sub"]), sitzung=sid)

    def abmelden(self, cookies: dict) -> str:
        """Sitzung beenden. Liefert die Adresse, zu der die Oberfläche danach geht."""
        token, hinweis = (cookies.get(self.s.cookie) if self.s else None), ""
        if token:
            row = self.store.sitzung(_hash(token))
            if row:
                self.store.sitzung_loeschen(row["id"])
                if row["art"] == "nutzer":
                    hinweis = str(row["daten"].get("id_token") or "")
                    self.store.audit(row["name"], "abmeldung")
        return (self.oidc.abmelde_adresse(hinweis) if self.oidc else "") or "/abgemeldet"

    def cookie(self, name: str, wert: str, dauer: int) -> str:
        teile = [f"{name}={wert}", "Path=/", f"Max-Age={dauer}", "HttpOnly", "SameSite=Lax"]
        if self.s.https:
            teile.append("Secure")
        return "; ".join(teile)


# --------------------------------------------------------------------------
# Seiten ohne Oberfläche: Fehler bei der Anmeldung, abgemeldet
# --------------------------------------------------------------------------

def seite(titel: str, text: str, knopf: str = "Anmelden", ziel: str = "/anmelden") -> str:
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(titel)} – Stellwerk</title><style>
@font-face{{font-family:"Barlow Semi Condensed";font-weight:400;src:url(/fonts/barlow-semi-condensed-latin-400-normal.woff2) format("woff2")}}
@font-face{{font-family:"Barlow Semi Condensed";font-weight:600;src:url(/fonts/barlow-semi-condensed-latin-600-normal.woff2) format("woff2")}}
body{{margin:0;background:#E2E5E8;color:#000;font:17px/1.45 "Barlow Semi Condensed","Arial Narrow",Arial,sans-serif}}
header{{background:#fff;border-bottom:2px solid #000;padding:10px 20px;font-weight:600;font-size:20px}}
main{{background:#fff;max-width:520px;margin:48px auto;padding:24px 28px}}h1{{font-size:24px;font-weight:600;margin:0 0 10px}}p{{margin:0 0 20px}}
a.k{{display:inline-block;background:#000;color:#fff;text-decoration:none;font-weight:600;padding:8px 18px;border-radius:3px}}
a.k:hover{{background:#333}}a.k:focus-visible{{outline:2px solid #0057D9;outline-offset:2px}}
</style></head><body><header>Stellwerk</header><main><h1>{html.escape(titel)}</h1><p>{html.escape(text)}</p>
<a class="k" href="{html.escape(ziel)}">{html.escape(knopf)}</a></main></body></html>"""
