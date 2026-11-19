"""Nachgebauter Identitätsdienst für die Tests: OpenID Connect mit Authorization Code, PKCE und RS256.

Er läuft als echter HTTP-Server auf 127.0.0.1, damit Stellwerk ihn so anspricht wie Keycloak oder AD FS.
Über `fehler` lässt er sich gezielt falsch verhalten (falsche Signatur, fremder Empfänger, abgelaufen ...).
Der Schlüssel wird je Testlauf neu erzeugt; im Repo liegt kein privater Schlüssel.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_KLEIN = [p for p in range(3, 2000, 2) if all(p % q for q in range(3, int(p ** 0.5) + 1, 2))]
_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _ist_prim(n: int, runden: int = 12) -> bool:
    if any(n % p == 0 for p in _KLEIN):
        return False
    d, r = n - 1, 0
    while d % 2 == 0:
        d, r = d // 2, r + 1
    for _ in range(runden):
        x = pow(secrets.randbelow(n - 3) + 2, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prim(bits: int) -> int:
    while True:
        c = secrets.randbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if _ist_prim(c):
            return c


def rsa_schluessel(bits: int = 2048) -> tuple[int, int, int]:
    e = 65537
    while True:
        p, q = _prim(bits // 2), _prim(bits // 2)
        phi = (p - 1) * (q - 1)
        if p != q and phi % e:
            return p * q, e, pow(e, -1, phi)


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def rs256(daten: bytes, n: int, d: int) -> bytes:
    k = (n.bit_length() + 7) // 8
    t = _PREFIX + hashlib.sha256(daten).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")


class Idp:
    def __init__(self, schluessel=None, fremd=None):
        self.n, self.e, self.d = schluessel or rsa_schluessel()
        self.fremd = fremd                                   # zweiter Schlüssel für „falsch signiert“
        self.kid = "test-1"
        self.client_id, self.client_secret = "stellwerk", "geheim-nur-im-test"
        self.nutzer: dict = {}
        self.nutzerinfo: dict = {}
        self.fehler: dict = {}
        self.codes: dict = {}
        self.letzte_anfrage: dict = {}
        self.abrufe: list[str] = []
        idp = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                raw = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                idp.abrufe.append(u.path)
                if u.path == "/.well-known/openid-configuration":
                    return self._json(idp.beschreibung())
                if u.path == "/jwks":
                    return self._json({"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": idp.kid,
                                                 "n": b64(idp.n.to_bytes((idp.n.bit_length() + 7) // 8, "big")),
                                                 "e": b64(idp.e.to_bytes(3, "big"))}]})
                if u.path == "/auth":                        # der Nutzer „meldet sich an“: sofort zurück mit Code
                    idp.letzte_anfrage = q
                    if idp.fehler.get("ablehnen"):
                        ziel = q["redirect_uri"] + "?" + urllib.parse.urlencode({"error": "access_denied", "state": q["state"],
                                                                                 "error_description": "Nutzer hat <b>abgelehnt</b>"})
                    else:
                        code = secrets.token_urlsafe(16)
                        idp.codes[code] = {"nonce": q.get("nonce", ""), "challenge": q.get("code_challenge", ""),
                                           "redirect_uri": q["redirect_uri"], "nutzer": dict(idp.nutzer)}
                        ziel = q["redirect_uri"] + "?" + urllib.parse.urlencode({"code": code, "state": q["state"]})
                    self.send_response(302)
                    self.send_header("Location", ziel)
                    self.end_headers()
                    return None
                if u.path == "/userinfo":
                    if self.headers.get("Authorization", "").startswith("Bearer at-"):
                        return self._json(idp.nutzerinfo)
                    return self._json({"error": "invalid_token"}, 401)
                return self._json({"error": "not_found"}, 404)

            def do_POST(self):
                u = urllib.parse.urlsplit(self.path)
                f = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()))
                if u.path != "/token":
                    return self._json({"error": "not_found"}, 404)
                basic = "Basic " + base64.b64encode(f"{idp.client_id}:{idp.client_secret}".encode()).decode()
                if not hmac.compare_digest(self.headers.get("Authorization", ""), basic):
                    return self._json({"error": "invalid_client", "error_description": "Client-Geheimnis stimmt nicht"}, 401)
                c = idp.codes.pop(f.get("code", ""), None)            # jeder Code gilt einmal
                soll = b64(hashlib.sha256(f.get("code_verifier", "").encode()).digest())
                if not c or c["redirect_uri"] != f.get("redirect_uri") or not c["challenge"] or soll != c["challenge"]:
                    return self._json({"error": "invalid_grant", "error_description": "Code oder PKCE stimmt nicht"}, 400)
                return self._json({"access_token": "at-" + secrets.token_urlsafe(8), "token_type": "Bearer",
                                   "id_token": idp.id_token(c["nutzer"], c["nonce"])})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def beschreibung(self) -> dict:
        return {"issuer": self.fehler.get("aussteller_in_beschreibung", self.url), "authorization_endpoint": self.url + "/auth",
                "token_endpoint": self.url + "/token", "jwks_uri": self.url + "/jwks", "userinfo_endpoint": self.url + "/userinfo",
                "end_session_endpoint": self.url + "/logout"}

    def id_token(self, nutzer: dict, nonce: str) -> str:
        f, now = self.fehler, int(time.time())
        claims = {"iss": f.get("iss", self.url), "aud": f.get("aud", self.client_id), "exp": now + f.get("exp", 300), "iat": now,
                  "nonce": f.get("nonce", nonce), **nutzer}
        if "azp" in f:
            claims["azp"] = f["azp"]
        kopf = {"alg": f.get("alg", "RS256"), "typ": "JWT", "kid": self.kid}
        teil = b64(json.dumps(kopf).encode()) + "." + b64(json.dumps(claims).encode())
        if kopf["alg"] == "none":
            return teil + "."
        if kopf["alg"] == "HS256":                           # Verwechslung: öffentlicher Schlüssel als HMAC-Geheimnis
            return teil + "." + b64(hmac.new(self.n.to_bytes(256, "big"), teil.encode(), hashlib.sha256).digest())
        n, d = (self.fremd[0], self.fremd[2]) if f.get("fremder_schluessel") else (self.n, self.d)
        sig = rs256(teil.encode(), n, d)
        if f.get("inhalt_geaendert"):                        # nach dem Signieren eine Gruppe untergeschoben
            claims["groups"] = list(claims.get("groups") or []) + ["nachtraeglich-dazu"]
            teil = teil.split(".")[0] + "." + b64(json.dumps(claims).encode())
        return teil + "." + b64(sig)

    def besuchen(self, url: str) -> str:
        """Der Browser des Nutzers ruft die Anmeldeseite auf. Liefert, wohin der Dienst ihn zurückschickt."""
        class Kein(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        try:
            urllib.request.build_opener(urllib.request.ProxyHandler({}), Kein).open(url, timeout=5)
        except urllib.error.HTTPError as exc:
            return exc.headers["Location"]
        raise AssertionError("keine Weiterleitung")

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
