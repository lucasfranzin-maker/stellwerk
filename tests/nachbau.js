// Nachbau der Schnittstelle nur aus LIESMICH.md und zuordnung.json, so wie ein Kanal-Skript es täte.
const fs = require('fs'), path = require('path'), crypto = require('crypto');
const dir = process.argv[2];
const Z = JSON.parse(fs.readFileSync(path.join(dir, 'zuordnung.json'), 'utf8'));
const ns = Z.ziel.namensraeume, V2 = 'http://terminology.hl7.org/CodeSystem/v2-0203';
const cols = z => Z.spalten.filter(s => s.stand === 'zugeordnet' && s.ziel === z);
const one = z => (cols(z)[0] || {}).spalte;
const cell = (row, c) => (c == null || row[c] == null) ? '' : String(row[c]).trim();
const pad = n => String(n).padStart(2, '0');

function versatz(y, mo, d, h, mi, s) {                     // Versatz der Zeitzone zum Ortszeitpunkt
  const utc = Date.UTC(y, mo - 1, d, h, mi, s);
  const f = new Intl.DateTimeFormat('en-GB', { timeZone: Z.ziel.zeitzone, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const lokal = t => { const p = Object.fromEntries(f.formatToParts(new Date(t)).map(x => [x.type, x.value])); return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second); };
  let min = Math.round((lokal(utc) - utc) / 60000);
  min = Math.round((lokal(utc - min * 60000) - (utc - min * 60000)) / 60000);
  const a = Math.abs(min);
  return (min < 0 ? '-' : '+') + pad(Math.floor(a / 60)) + ':' + pad(a % 60);
}
function datum(v) {
  let m = /^(\d{1,2})\.(\d{1,2})\.(\d{4})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$/.exec(v)
  let y, mo, d, h, mi, s;
  if (m) [, d, mo, y, h, mi, s] = m;
  else if ((m = /^(\d{4})-?(\d{2})-?(\d{2})(?:[ T]?(\d{2}):?(\d{2})(?::?(\d{2}))?)?/.exec(v))) [, y, mo, d, h, mi, s] = m;
  else return '';
  const tag = `${y}-${pad(+mo)}-${pad(+d)}`;
  if (h == null) return tag;
  return `${tag}T${pad(+h)}:${mi}:${s || '00'}` + versatz(+y, +mo, +d, +h, +mi, +(s || 0));
}

function bundle(row) {
  const pid = cell(row, one('patient.id'));
  if (!pid) return null;                                                                     // Regel 1
  const patUrl = 'urn:uuid:' + crypto.randomUUID();
  const pat = { resourceType: 'Patient', identifier: [{ type: { coding: [{ system: V2, code: 'MR' }] }, system: ns.patient, value: pid }] };
  const fam = cell(row, one('patient.family')), giv = cell(row, one('patient.given'));
  if (fam) pat.name = [Object.assign({ use: 'official', family: fam }, giv ? { given: giv.split(/\s+/) } : {})];
  const sex = cell(row, one('patient.gender'));
  if (sex) pat.gender = Z.geschlecht_codes[sex.toLowerCase()] || 'unknown';
  const geb = datum(cell(row, one('patient.birthDate'))).slice(0, 10);
  if (geb) pat.birthDate = geb;
  const entry = [{ fullUrl: patUrl, resource: pat, request: { method: 'POST', url: 'Patient', ifNoneExist: 'identifier=' + encodeURIComponent(ns.patient + '|' + pid) } }];
  const zeit = datum(cell(row, one('obs.zeit'))), fall = cell(row, one('fall.id')), auftrag = cell(row, one('auftrag.id'));
  let messungen = cols('obs.wert');
  if (one('obs.code')) {                                                                     // Zeilenform (Regel 5)
    const code = cell(row, one('obs.code')), e = cols('obs.code')[0].codes.find(x => x.code === code);
    messungen = e ? [{ spalte: code, wertspalte: messungen[0].spalte, loinc: e.loinc, anzeige: e.anzeige, einheit_spalte: messungen[0].einheit_spalte, einheit_fest: e.einheit_fest }] : [];
  }
  for (const w of messungen) {
    const raw = cell(row, w.wertspalte || w.spalte);
    if (!raw) continue;
    const coding = { system: 'http://loinc.org', code: w.loinc };
    if (w.anzeige) coding.display = w.anzeige;
    const obs = { resourceType: 'Observation', status: Z.ziel.observation_status,
      category: [{ coding: [{ system: 'http://terminology.hl7.org/CodeSystem/observation-category', code: 'laboratory' }] }],
      code: { coding: [coding], text: w.spalte }, subject: { reference: patUrl } };
    if (fall) obs.encounter = { identifier: { type: { coding: [{ system: V2, code: 'VN' }] }, system: ns.fall, value: fall } };
    if (zeit) obs.effectiveDateTime = zeit;
    const unit = cell(row, w.einheit_spalte || null) || w.einheit_fest;
    if (/^-?\d+(?:[.,]\d+)?$/.test(raw)) obs.valueQuantity = Object.assign({ value: '@@zahl:' + raw.replace(',', '.') + '@@' }, unit ? { unit } : {});
    else obs.valueString = raw + (unit ? ' ' + unit : '');
    let request = { method: 'POST', url: 'Observation' };
    if (auftrag) {
      const val = auftrag + '-' + w.loinc;
      obs.identifier = [{ system: ns.auftrag, value: val }];
      request = { method: 'PUT', url: 'Observation?identifier=' + encodeURIComponent(ns.auftrag + '|' + val) };
    }
    entry.push({ fullUrl: 'urn:uuid:' + crypto.randomUUID(), resource: obs, request });
  }
  return entry.length > 1 ? { resourceType: 'Bundle', type: 'transaction', entry } : null;
}

const row = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
process.stdout.write(JSON.stringify(bundle(row)).replace(/"@@zahl:(-?\d+(?:\.\d+)?)@@"/g, '$1') + '\n');   // Zahl als Text einsetzen (Regel 8)
