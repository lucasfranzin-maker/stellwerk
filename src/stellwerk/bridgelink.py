"""Anbindung an BridgeLink (Open-Source-Fork von Mirth Connect, Innovar Healthcare).

Stellwerk übergibt fertige FHIR-Bundles per HTTP an einen BridgeLink-Kanal. BridgeLink übernimmt das Routing:
an das FHIR-Ziel (CDR) mit Warteschlange und Wiederholung, Bundles mit Prüfpunkten in eine Prüfablage.
Weitere Ziele legt man in BridgeLink selbst an.

Routing-Merkmale stehen in HTTP-Kopfzeilen (keine Patientendaten):
  X-Stellwerk-Bundle-Typ   document | transaction
  X-Stellwerk-Status       ok | pruefen
  X-Stellwerk-Inhalt       Composition | Observation | ... (Ressourcentypen, kommagetrennt)
  X-Stellwerk-Dokumenttyp  KDL-Code der Composition, falls vorhanden
  X-Stellwerk-Quelle       sendendes System (MSH-3) oder Art der Quelle
  X-Request-ID             Kennung zum Nachverfolgen

Kanalformat und API wie in BridgeLink v26.9.0 (getestet gegen einen Build aus dem Quelltext).
"""
from __future__ import annotations

import base64
import http.cookiejar
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from xml.sax.saxutils import escape

KANAL_NS = uuid.UUID("5b7a1c3e-2f44-4d1b-9c6e-8a0f3d2b1e77")


class BridgeLinkError(Exception):
    pass


# --------------------------------------------------------------------------
# Routing-Merkmale
# --------------------------------------------------------------------------

def routing_headers(bundle: dict, status: str, quelle: str = "", request_id: str = "") -> dict[str, str]:
    res = [e.get("resource", {}) for e in bundle.get("entry", [])]
    typen = sorted({r.get("resourceType", "") for r in res if r.get("resourceType") not in ("Patient", "Encounter")})
    kdl = ""
    for r in res:
        if r.get("resourceType") == "Composition":
            kdl = next((c.get("code", "") for c in (r.get("type") or {}).get("coding", [])
                        if c.get("system") == "http://dvmd.de/fhir/CodeSystem/kdl"), "")
    h = {"X-Stellwerk-Bundle-Typ": bundle.get("type", ""), "X-Stellwerk-Status": status,
         "X-Stellwerk-Inhalt": ",".join(typen), "X-Request-ID": request_id or str(uuid.uuid4())}
    if kdl:
        h["X-Stellwerk-Dokumenttyp"] = kdl
    if quelle:
        h["X-Stellwerk-Quelle"] = "".join(ch for ch in quelle if 32 <= ord(ch) < 127)[:60]
    return h


# --------------------------------------------------------------------------
# Kanalvorlage
# --------------------------------------------------------------------------

def _raw(tag: str) -> str:
    cls = "com.mirth.connect.plugins.datatypes.raw"
    props = (f'<batchProperties class="{cls}.RawBatchProperties" version="{{v}}"><splitType>JavaScript</splitType>'
             f'<batchScript></batchScript></batchProperties>')
    return (f'<{tag} version="{{v}}"><elements/><inboundDataType>RAW</inboundDataType><outboundDataType>RAW</outboundDataType>'
            f'<inboundProperties class="{cls}.RawDataTypeProperties" version="{{v}}">{props}</inboundProperties>'
            f'<outboundProperties class="{cls}.RawDataTypeProperties" version="{{v}}">{props}</outboundProperties></{tag}>')


def _resources() -> str:
    return ('<resourceIds class="linked-hash-map"><entry><string>Default Resource</string>'
            '<string>[Default Resource]</string></entry></resourceIds>')


def _dest_props(queue: bool) -> str:
    return (f'<destinationConnectorProperties version="{{v}}"><queueEnabled>{str(queue).lower()}</queueEnabled>'
            '<sendFirst>true</sendFirst><retryIntervalMillis>60000</retryIntervalMillis><regenerateTemplate>false</regenerateTemplate>'
            f'<retryCount>{"0" if queue else "3"}</retryCount><rotate>false</rotate><includeFilterTransformer>false</includeFilterTransformer>'
            '<threadCount>1</threadCount><threadAssignmentVariable></threadAssignmentVariable><validateResponse>false</validateResponse>'
            f'{_resources()}<queueBufferSize>1000</queueBufferSize><reattachAttachments>true</reattachAttachments>'
            '</destinationConnectorProperties>')


def _filter(name: str, script: str) -> str:
    return ('<filter version="{v}"><elements><com.mirth.connect.plugins.javascriptrule.JavaScriptRule version="{v}">'
            f'<name>{escape(name)}</name><sequenceNumber>0</sequenceNumber><enabled>true</enabled><operator>NONE</operator>'
            f'<script>{escape(script)}</script></com.mirth.connect.plugins.javascriptrule.JavaScriptRule></elements></filter>')


def _connector(meta_id: int, name: str, props: str, transport: str, filt: str) -> str:
    return (f'<connector version="{{v}}"><metaDataId>{meta_id}</metaDataId><name>{escape(name)}</name>{props}'
            f'{_raw("transformer")}{_raw("responseTransformer")}{filt}<transportName>{transport}</transportName>'
            '<mode>DESTINATION</mode><enabled>true</enabled><waitForPrevious>true</waitForPrevious></connector>')


def channel_xml(k: dict, listener_user: str, listener_password: str, version: str = "26.9.0") -> tuple[str, str]:
    """Kanal „Stellwerk FHIR-Eingang“ als BridgeLink-Export. Liefert (Kanal-ID, XML)."""
    name = k.get("name") or "Stellwerk FHIR-Eingang"
    cid = str(uuid.uuid5(KANAL_NS, name))
    pfad = "/" + (k.get("pfad") or "/stellwerk").strip("/")
    ziel = (k.get("fhir_ziel") or "").strip()
    ablage = k.get("pruefablage") or "/opt/bridgelink/appdata/stellwerk/pruefen"
    if not listener_password:
        raise BridgeLinkError("Für den Kanal fehlt das Passwort der Zustellung (Umgebungsvariable aus zustellung.passwort_env).")

    source = (
        '<sourceConnector version="{v}"><metaDataId>0</metaDataId><name>sourceConnector</name>'
        '<properties class="com.mirth.connect.connectors.http.HttpReceiverProperties" version="{v}"><pluginProperties>'
        '<com.mirth.connect.plugins.httpauth.basic.BasicHttpAuthProperties version="{v}"><authType>BASIC</authType>'
        f'<realm>stellwerk</realm><credentials class="linked-hash-map"><entry><string>{escape(listener_user)}</string>'
        f'<string>{escape(listener_password)}</string></entry></credentials><isUseCredentialsVariable>false</isUseCredentialsVariable>'
        '<credentialsVariable></credentialsVariable></com.mirth.connect.plugins.httpauth.basic.BasicHttpAuthProperties></pluginProperties>'
        f'<listenerConnectorProperties version="{{v}}"><host>{escape(k.get("host") or "0.0.0.0")}</host>'
        f'<port>{int(k.get("port") or 8095)}</port></listenerConnectorProperties>'
        '<sourceConnectorProperties version="{v}"><responseVariable>Auto-generate (Destinations completed)</responseVariable>'
        '<respondAfterProcessing>true</respondAfterProcessing><processBatch>false</processBatch><firstResponse>false</firstResponse>'
        f'<processingThreads>1</processingThreads>{_resources()}<queueBufferSize>1000</queueBufferSize></sourceConnectorProperties>'
        '<xmlBody>false</xmlBody><parseMultipart>false</parseMultipart><includeMetadata>false</includeMetadata>'
        '<binaryMimeTypes>application/.*(?&lt;!json|xml)$|image/.*|video/.*|audio/.*</binaryMimeTypes>'
        '<binaryMimeTypesRegex>true</binaryMimeTypesRegex><responseContentType>text/plain</responseContentType>'
        '<responseDataTypeBinary>false</responseDataTypeBinary><responseStatusCode></responseStatusCode>'
        '<responseHeaders class="linked-hash-map"/><responseHeadersVariable></responseHeadersVariable>'
        f'<useResponseHeadersVariable>false</useResponseHeadersVariable><charset>UTF-8</charset><contextPath>{escape(pfad)}</contextPath>'
        '<timeout>30000</timeout><requestHeaderSize>8192</requestHeaderSize><staticResources/></properties>'
        + _raw("transformer").replace("<elements/>", '<elements><com.mirth.connect.plugins.javascriptstep.JavaScriptStep version="{v}">'
                                      '<name>Routing-Merkmale lesen</name><sequenceNumber>0</sequenceNumber><enabled>true</enabled><script>'
                                      + escape(_SOURCE_SCRIPT) + '</script></com.mirth.connect.plugins.javascriptstep.JavaScriptStep></elements>')
        + _filter("Nur Bundles von Stellwerk", "return String($('headers').getHeader('X-Stellwerk-Bundle-Typ') || '') != '';")
        + '<transportName>HTTP Listener</transportName><mode>SOURCE</mode><enabled>true</enabled><waitForPrevious>true</waitForPrevious>'
          '</sourceConnector>')

    dests = []
    if ziel:
        auth = k.get("fhir_auth") or "keine"
        user = k.get("fhir_benutzer") or ""
        pw = os.environ.get(k.get("fhir_passwort_env") or "", "") if auth == "basic" else ""
        headers = '<headers class="linked-hash-map"><entry><string>Accept</string><list><string>application/fhir+json</string></list></entry>'
        if auth == "bearer":
            tok = os.environ.get(k.get("fhir_token_env") or "", "")
            if not tok:
                raise BridgeLinkError("fhir_auth ist bearer, aber das Token fehlt (Umgebungsvariable aus fhir_token_env).")
            headers += f'<entry><string>Authorization</string><list><string>Bearer {escape(tok)}</string></list></entry>'
        headers += '</headers>'
        http = (
            '<properties class="com.mirth.connect.connectors.http.HttpDispatcherProperties" version="{v}"><pluginProperties/>'
            + _dest_props(True) +
            f'<host>{escape(ziel)}</host><useProxyServer>false</useProxyServer><proxyAddress></proxyAddress><proxyPort></proxyPort>'
            f'<method>post</method>{headers}<parameters class="linked-hash-map"/><useHeadersVariable>false</useHeadersVariable>'
            '<headersVariable></headersVariable><useParametersVariable>false</useParametersVariable><parametersVariable></parametersVariable>'
            '<responseXmlBody>false</responseXmlBody><responseParseMultipart>false</responseParseMultipart>'
            '<responseIncludeMetadata>false</responseIncludeMetadata>'
            '<responseBinaryMimeTypes>application/.*(?&lt;!json|xml)$|image/.*|video/.*|audio/.*</responseBinaryMimeTypes>'
            '<responseBinaryMimeTypesRegex>true</responseBinaryMimeTypesRegex><multipart>false</multipart>'
            f'<useAuthentication>{"true" if auth == "basic" else "false"}</useAuthentication><authenticationType>Basic</authenticationType>'
            f'<usePreemptiveAuthentication>true</usePreemptiveAuthentication><username>{escape(user)}</username><password>{escape(pw)}</password>'
            '<content>${message.encodedData}</content><contentType>application/fhir+json</contentType><dataTypeBinary>false</dataTypeBinary>'
            '<charset>UTF-8</charset><socketTimeout>60000</socketTimeout></properties>')
        dests.append(_connector(1, "FHIR-Ziel", http, "HTTP Sender",
                                _filter("Freigegebene Bundles", "return $('stellwerkStatus') == 'ok';")))
    datei = (
        '<properties class="com.mirth.connect.connectors.file.FileDispatcherProperties" version="{v}"><pluginProperties/>'
        + _dest_props(False) +
        f'<scheme>FILE</scheme><host>{escape(ablage)}</host><outputPattern>${{stellwerkDateiname}}</outputPattern><anonymous>false</anonymous>'
        '<username></username><password></password><timeout>10000</timeout><keepConnectionOpen>true</keepConnectionOpen>'
        '<maxIdleTime>0</maxIdleTime><secure>true</secure><passive>true</passive><validateConnection>true</validateConnection>'
        '<outputAppend>false</outputAppend><errorOnExists>false</errorOnExists><temporary>true</temporary><binary>false</binary>'
        '<charsetEncoding>UTF-8</charsetEncoding><template>${message.encodedData}</template></properties>')
    regel = "return $('stellwerkStatus') != 'ok';" if ziel else "return true;"
    dests.append(_connector(len(dests) + 1, "Prüfablage" if ziel else "Ablage", datei, "File Writer",
                            _filter("Bundles mit Prüfpunkten" if ziel else "Alle Bundles", regel)))

    meta = "".join(f'<metaDataColumn><name>{n}</name><type>STRING</type><mappingName>{m}</mappingName></metaDataColumn>'
                   for n, m in (("TYP", "stellwerkTyp"), ("STATUS", "stellwerkStatus"), ("QUELLE", "stellwerkQuelle"),
                                ("DOKUMENTTYP", "stellwerkDokumenttyp")))
    beschreibung = ("Nimmt FHIR-Bundles von Stellwerk an (HTTP, Basic-Auth) und leitet sie weiter. "
                    "Freigegebene Bundles gehen an das FHIR-Ziel, Bundles mit Prüfpunkten in die Prüfablage. "
                    "Von Stellwerk erzeugt; Änderungen hier werden bei erneutem Einrichten überschrieben.")
    xml = (
        f'<channel version="{{v}}"><id>{cid}</id><nextMetaDataId>{len(dests) + 1}</nextMetaDataId><name>{escape(name)}</name>'
        f'<description>{escape(beschreibung)}</description><revision>1</revision>{source}'
        f'<destinationConnectors>{"".join(dests)}</destinationConnectors>'
        '<preprocessingScript>return message;</preprocessingScript><postprocessingScript>return;</postprocessingScript>'
        '<deployScript>return;</deployScript><undeployScript>return;</undeployScript>'
        '<properties version="{v}"><clearGlobalChannelMap>true</clearGlobalChannelMap>'
        f'<messageStorageMode>{escape(k.get("speicherung") or "PRODUCTION")}</messageStorageMode>'
        f'<encryptData>{str(bool(k.get("verschluesseln", True))).lower()}</encryptData><encryptAttachments>true</encryptAttachments>'
        '<encryptCustomMetaData>false</encryptCustomMetaData><removeContentOnCompletion>false</removeContentOnCompletion>'
        '<removeOnlyFilteredOnCompletion>false</removeOnlyFilteredOnCompletion><removeAttachmentsOnCompletion>false</removeAttachmentsOnCompletion>'
        f'<initialState>STARTED</initialState><storeAttachments>true</storeAttachments><metaDataColumns>{meta}</metaDataColumns>'
        '<attachmentProperties version="{v}"><type>None</type><properties/></attachmentProperties>'
        f'{_resources()}</properties><exportData><metadata><enabled>true</enabled><pruningSettings><archiveEnabled>true</archiveEnabled>'
        '<pruneErroredMessages>false</pruneErroredMessages></pruningSettings></metadata></exportData></channel>')
    return cid, xml.replace("{v}", version)


_SOURCE_SCRIPT = """\
var h = $('headers');
function kopf(n) { var v = h.getHeader(n); return v == null ? '' : String(v); }
var status = kopf('X-Stellwerk-Status');
channelMap.put('stellwerkTyp', kopf('X-Stellwerk-Bundle-Typ'));
channelMap.put('stellwerkStatus', status == 'ok' ? 'ok' : 'pruefen');
channelMap.put('stellwerkQuelle', kopf('X-Stellwerk-Quelle'));
channelMap.put('stellwerkDokumenttyp', kopf('X-Stellwerk-Dokumenttyp'));
var id = kopf('X-Request-ID').replace(/[^A-Za-z0-9_-]/g, '');
channelMap.put('stellwerkDateiname', (id || String(connectorMessage.getMessageId())) + '.json');
"""


# --------------------------------------------------------------------------
# Verwaltungsschnittstelle (REST-API von BridgeLink)
# --------------------------------------------------------------------------

class BridgeLink:
    def __init__(self, cfg: dict):
        self.api = (cfg.get("api") or "").rstrip("/")
        if not self.api.startswith(("https://", "http://")):
            raise BridgeLinkError("bridgelink.api fehlt, z. B. https://bridgelink:8443/api")
        self.user = cfg.get("benutzer") or ""
        self.password = os.environ.get(cfg.get("passwort_env") or "STELLWERK_BRIDGELINK_PASSWORT", "")
        if not self.user or not self.password:
            raise BridgeLinkError("Benutzer (bridgelink.benutzer) oder Passwort (Umgebungsvariable aus bridgelink.passwort_env) fehlt.")
        ctx = ssl.create_default_context(cafile=cfg.get("ca") or None)
        if cfg.get("tls_pruefen") is False:                        # nur für Tests mit selbstsigniertem Zertifikat
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self._open = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx),
                                                 urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())).open
        self._logged_in = False

    def _call(self, method: str, path: str, data: bytes | None = None, ctype: str = "application/xml", accept: str = "application/json"):
        req = urllib.request.Request(self.api + path, data=data, method=method,
                                     headers={"X-Requested-With": "Stellwerk", "Accept": accept, **({"Content-Type": ctype} if data is not None else {})})
        try:
            with self._open(req, timeout=60) as r:
                body = r.read()
                return r.status, body
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            raise BridgeLinkError(f"BridgeLink antwortet auf {method} {path} mit HTTP {exc.code}: {body}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise BridgeLinkError(f"BridgeLink unter {self.api} nicht erreichbar ({getattr(exc, 'reason', exc)}).") from exc

    def login(self) -> None:
        if self._logged_in:
            return
        data = urllib.parse.urlencode({"username": self.user, "password": self.password}).encode()
        _, body = self._call("POST", "/users/_login", data, "application/x-www-form-urlencoded")
        status = json.loads(body or b"{}").get("com.mirth.connect.model.LoginStatus", {}).get("status", "")
        if status not in ("SUCCESS", "SUCCESS_GRACE_PERIOD"):
            raise BridgeLinkError(f"Anmeldung an BridgeLink fehlgeschlagen ({status or 'keine Antwort'}).")
        self._logged_in = True

    def version(self) -> str:
        _, body = self._call("GET", "/server/version", accept="text/plain")
        return body.decode().strip().strip('"')

    def install(self, cid: str, xml: str) -> str:
        """Kanal anlegen oder ersetzen und bereitstellen. Liefert 'angelegt' oder 'aktualisiert'."""
        self.login()
        _, body = self._call("GET", "/channels/idsAndNames")
        vorhanden = cid in body.decode("utf-8", "replace")
        if vorhanden:
            self._call("PUT", f"/channels/{cid}?override=true", xml.encode("utf-8"))
        else:
            self._call("POST", "/channels", xml.encode("utf-8"))
        self._call("POST", f"/channels/{cid}/_deploy?returnErrors=true")
        return "aktualisiert" if vorhanden else "angelegt"

    def status(self, cid: str) -> dict:
        """Zustand je Verbindung aus dem Dashboard von BridgeLink (gegen 26.9.0 geprüft).

        "gefiltert" heißt bei den Zielen nur "nicht zuständig" (OK-Bundles gehen nicht in die Prüfablage
        und umgekehrt) und wird deshalb nicht gezählt. An der Quelle heißt es: abgewiesen, weil das
        Routing-Merkmal fehlte."""
        self.login()
        _, body = self._call("GET", f"/channels/{cid}/status")
        st = json.loads(body or b"{}").get("dashboardStatus", {})
        teile = (st.get("childStatuses") or {}).get("dashboardStatus") or []
        teile = teile if isinstance(teile, list) else [teile]
        src = next((t for t in teile if t.get("metaDataId") == 0), {})
        ziele = [{"name": t.get("name", ""), "zustand": t.get("state", ""), "gesendet": _zahl(t, "SENT"),
                  "fehler": _zahl(t, "ERROR"), "warteschlange": int(t.get("queued") or 0)}
                 for t in teile if t.get("metaDataId") not in (0, None)]
        return {"zustand": st.get("state", "unbekannt"), "name": st.get("name", ""),
                "empfangen": _zahl(src, "RECEIVED"), "abgewiesen": _zahl(src, "FILTERED"),
                "gesendet": sum(z["gesendet"] for z in ziele), "fehler": _zahl(st, "ERROR"),
                "warteschlange": sum(z["warteschlange"] for z in ziele), "ziele": ziele}


def _zahl(status: dict, art: str) -> int:
    e = ((status.get("statistics") or {}).get("entry")) or []
    for x in e if isinstance(e, list) else [e]:
        if x.get("com.mirth.connect.donkey.model.message.Status") == art:
            return int(x.get("long") or 0)
    return 0


def basic_auth(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
