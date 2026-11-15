/*
 * Stellwerk: FHIR-Bundles aus Datenbankzeilen bauen.
 *
 * Setzt die Regeln aus LIESMICH.md des Übergabepakets um (Feld-Mapping). Reines ES5, damit dieselbe Datei im
 * BridgeLink-Kanal (Rhino) und unter Node läuft. Keine Abhängigkeit außer der Umgebung `env`:
 *   env.offset(zone, jahr, monat, tag, stunde, minute, sekunde) -> "+01:00"   Versatz der Zeitzone (siehe unten)
 *   env.uuid() -> "xxxxxxxx-xxxx-..."
 *
 * Regel für den Versatz: In Lücke und Überlappung der Zeitumstellung gilt der Versatz vor der Umstellung
 * (wie Python zoneinfo mit fold=0), damit Stellwerk und Kanal denselben Zeitpunkt schreiben.
 */
var StellwerkBauen = (function () {
  'use strict';
  var V2 = 'http://terminology.hl7.org/CodeSystem/v2-0203';
  var TS = /^(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?(?:\.\d+)?([+-]\d{4})?$/;
  var ZAHL = /^-?\d+(?:[.,]\d+)?$/;

  function pad(n) { n = String(n); return n.length < 2 ? '0' + n : n; }
  function leer(v) { return v === null || v === undefined; }
  var hat = function (o, k) { return Object.prototype.hasOwnProperty.call(o, k); };
  function zelle(row, spalte) {
    if (!spalte) { return ''; }
    var k = String(spalte).toLowerCase();
    return hat(row, k) && !leer(row[k]) ? String(row[k]).trim() : '';
  }
  function kodiert(s) {                                   // wie Python urllib.parse.quote(s, safe="")
    return encodeURIComponent(s).replace(/[!'()*]/g, function (c) { return '%' + c.charCodeAt(0).toString(16).toUpperCase(); });
  }
  function tageImMonat(j, m) {
    return m === 2 ? ((j % 4 === 0 && j % 100 !== 0) || j % 400 === 0 ? 29 : 28) : (m === 4 || m === 6 || m === 9 || m === 11 ? 30 : 31);
  }

  // ---- Zeitangaben (wie sqlsource._date und hl7v2.ts_to_fhir)
  function tsZuFhir(ts, zone, env) {
    var m = TS.exec(String(ts || '').trim());
    if (!m) { return ''; }
    var y = m[1], mo = m[2], d = m[3], h = m[4], mi = m[5], s = m[6], off = m[7];
    if (!mo) { return y; }
    if (!d) { return y + '-' + mo; }
    if (!h) { return y + '-' + mo + '-' + d; }
    if (off) { return y + '-' + mo + '-' + d + 'T' + h + ':' + (mi || '00') + ':' + (s || '00') + off.slice(0, 3) + ':' + off.slice(3); }
    var J = +y, M = +mo, D = +d, H = +h, MI = +(mi || 0), S = +(s || 0);
    if (J < 1 || M < 1 || M > 12 || D < 1 || D > tageImMonat(J, M) || H > 23 || MI > 59 || S > 59) { return ''; }
    return y + '-' + mo + '-' + d + 'T' + h + ':' + (mi || '00') + ':' + (s || '00') + env.offset(zone, J, M, D, H, MI, S);
  }
  function datum(wert, zone, env) {
    var s = String(wert === null || wert === undefined ? '' : wert).trim();
    if (!s) { return ''; }
    var m = /^(\d{1,2})\.(\d{1,2})\.(\d{4})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$/.exec(s);
    if (m) {
      return tsZuFhir(m[3] + pad(+m[2]) + pad(+m[1]) + (m[4] ? pad(+m[4]) + m[5] + (m[6] || '00') : ''), zone, env);
    }
    return tsZuFhir(s.replace(/[-:T ]/g, '').slice(0, 14), zone, env);
  }

  // ---- Zeile aus dem XML des Database Readers lesen: <result><spalte>wert</spalte>...</result>
  function entitaeten(s) {
    return s.replace(/&(#x[0-9a-fA-F]+|#\d+|lt|gt|amp|quot|apos);/g, function (all, e) {
      if (e === 'lt') { return '<'; }
      if (e === 'gt') { return '>'; }
      if (e === 'amp') { return '&'; }
      if (e === 'quot') { return '"'; }
      if (e === 'apos') { return "'"; }
      var n = e.charAt(1) === 'x' ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
      return String.fromCharCode(n);
    });
  }
  function zeileLesen(xml) {
    var row = {}, m;
    var innen = /<result(?:\s[^>]*)?>([\s\S]*)<\/result>/.exec(String(xml));
    var re = /<([A-Za-z_][\w.\-]*)(?:\s[^>]*)?(?:\/>|>([\s\S]*?)<\/\1>)/g;
    var text = innen ? innen[1] : String(xml);
    while ((m = re.exec(text)) !== null) { row[m[1].toLowerCase()] = m[2] === undefined ? '' : entitaeten(m[2]); }
    return row;
  }

  // ---- Bundle bauen. Z ist zuordnung.json des Übergabepakets.
  function bauen(row, Z, env) {
    var ns = Z.ziel.namensraeume, zone = Z.ziel.zeitzone, sp = Z.spalten;
    var hinweise = [];
    function cols(ziel) { var r = []; for (var i = 0; i < sp.length; i++) { if (sp[i].stand === 'zugeordnet' && sp[i].ziel === ziel) { r.push(sp[i]); } } return r; }
    function eine(ziel) { var c = cols(ziel); return c.length ? c[0].spalte : null; }

    var pid = zelle(row, eine('patient.id'));
    if (!pid) { return { bundle: null, grund: 'KEINE_PATIENTENNUMMER', hinweise: hinweise }; }
    var patUrl = 'urn:uuid:' + env.uuid();
    var pat = { resourceType: 'Patient', identifier: [{ type: { coding: [{ system: V2, code: 'MR' }] }, system: ns.patient, value: pid }] };
    var fam = zelle(row, eine('patient.family')), giv = zelle(row, eine('patient.given'));
    if (fam) {
      pat.name = [{ use: 'official', family: fam }];
      if (giv) { pat.name[0].given = giv.split(/\s+/); }
    }
    var sex = zelle(row, eine('patient.gender'));
    if (sex) {
      var code = hat(Z.geschlecht_codes, sex.toLowerCase()) ? Z.geschlecht_codes[sex.toLowerCase()] : '';
      pat.gender = code || 'unknown';
      if (!code) { hinweise.push({ code: 'GESCHLECHT', text: 'Unbekannter Geschlechtscode, im Bundle steht „unknown“.' }); }
    }
    var gebSpalte = eine('patient.birthDate');
    if (gebSpalte && zelle(row, gebSpalte)) {
      var geb = datum(zelle(row, gebSpalte), zone, env).slice(0, 10);
      if (geb) { pat.birthDate = geb; } else { hinweise.push({ code: 'DATUM', text: 'Geburtsdatum nicht lesbar und deshalb weggelassen.' }); }
    }
    var entries = [{ fullUrl: patUrl, resource: pat, request: { method: 'POST', url: 'Patient', ifNoneExist: 'identifier=' + kodiert(ns.patient + '|' + pid) } }];

    var zeitSpalte = eine('obs.zeit'), zeit = '';
    if (zeitSpalte && zelle(row, zeitSpalte)) {
      zeit = datum(zelle(row, zeitSpalte), zone, env);
      if (!zeit) { hinweise.push({ code: 'DATUM', text: 'Zeitpunkt nicht lesbar und deshalb weggelassen.' }); }
    }
    var fall = zelle(row, eine('fall.id')), auftrag = zelle(row, eine('auftrag.id'));
    var werte = cols('obs.wert');
    for (var i = 0; i < werte.length; i++) {
      var w = werte[i], raw = zelle(row, w.spalte);
      if (!raw) { continue; }
      var coding = { system: 'http://loinc.org', code: w.loinc };
      if (w.anzeige) { coding.display = w.anzeige; }
      var obs = {
        resourceType: 'Observation', status: Z.ziel.observation_status,
        category: [{ coding: [{ system: 'http://terminology.hl7.org/CodeSystem/observation-category', code: 'laboratory' }] }],
        code: { coding: [coding], text: w.spalte }, subject: { reference: patUrl }
      };
      if (fall) { obs.encounter = { identifier: { type: { coding: [{ system: V2, code: 'VN' }] }, system: ns.fall, value: fall } }; }
      if (zeit) { obs.effectiveDateTime = zeit; }
      var einheit = zelle(row, w.einheit_spalte) || (w.einheit_fest || '').trim();
      if (ZAHL.test(raw)) {
        obs.valueQuantity = { value: '@@zahl:' + raw.replace(',', '.') + '@@' };
        if (einheit) { obs.valueQuantity.unit = einheit; }
      } else {
        obs.valueString = raw + (einheit ? ' ' + einheit : '');
        hinweise.push({ code: 'TEXTWERT', text: w.spalte + ': Wert ist keine reine Zahl und steht als Text (valueString) im Bundle.' });
      }
      var request;
      if (auftrag) {
        var val = auftrag + '-' + w.loinc;
        obs.identifier = [{ system: ns.auftrag, value: val }];
        request = { method: 'PUT', url: 'Observation?identifier=' + kodiert(ns.auftrag + '|' + val) };
      } else {
        request = { method: 'POST', url: 'Observation' };
      }
      entries.push({ fullUrl: 'urn:uuid:' + env.uuid(), resource: obs, request: request });
    }
    if (entries.length < 2) { return { bundle: null, grund: 'KEIN_MESSWERT', hinweise: hinweise }; }
    return { bundle: { resourceType: 'Bundle', type: 'transaction', entry: entries }, grund: '', hinweise: hinweise };
  }

  // Messwerte als Zahl mit ihren Nachkommastellen schreiben (1.10 bleibt 1.10, Regel 8 der Übergabe)
  function json(bundle) {
    return JSON.stringify(bundle).replace(/"@@zahl:(-?\d+(?:\.\d+)?)@@"/g, '$1');
  }

  return { bauen: bauen, json: json, zeileLesen: zeileLesen, datum: datum, tsZuFhir: tsZuFhir, kodiert: kodiert };
}());

if (typeof module !== 'undefined' && module.exports) { module.exports = StellwerkBauen; }
