// Läuft kanal_bauen.js (dieselbe Datei wie im BridgeLink-Kanal) über die Testfälle eines Übergabepakets.
// Aufruf: node kanal_nachbau.js <Paketordner>   Ausgabe: JSON {"testfaelle/<Name>": Bundle-Text | "null"}, bei echten Zeilen "echte-zeilen/<Name>"
const fs = require('fs'), path = require('path'), crypto = require('crypto');
const B = require('../src/stellwerk/kanal_bauen.js');
const dir = process.argv[2];
const Z = JSON.parse(fs.readFileSync(path.join(dir, 'zuordnung.json'), 'utf8'));

function versatzIntl() {                        // Versatz vor der Umstellung, wie Python zoneinfo (fold=0)
  const cache = {};
  const format = zone => cache[zone] || (cache[zone] = new Intl.DateTimeFormat('en-GB', { timeZone: zone, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }));
  const lokal = (f, t) => { const p = Object.fromEntries(f.formatToParts(new Date(t)).map(x => [x.type, x.value])); return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second); };
  return (zone, y, mo, d, h, mi, s) => {
    const f = format(zone), min = t => Math.round((lokal(f, t) - t) / 60000);
    const wand = Date.UTC(y, mo - 1, d, h, mi, s), o1 = min(wand - 864e5), o2 = min(wand + 864e5);
    const gilt = o => min(wand - o * 60000) === o;
    const o = gilt(o1) || !gilt(o2) ? o1 : o2, a = Math.abs(o), pad = n => String(n).padStart(2, '0');
    return (o < 0 ? '-' : '+') + pad(Math.floor(a / 60)) + ':' + pad(a % 60);
  };
}
const env = { offset: versatzIntl(), uuid: () => crypto.randomUUID() };
const klein = o => Object.fromEntries(Object.entries(o).map(([k, v]) => [k.toLowerCase(), v]));
const out = {};
for (const teil of ['testfaelle', 'echte-zeilen']) {
  if (!fs.existsSync(path.join(dir, teil))) continue;
  for (const slug of fs.readdirSync(path.join(dir, teil)).sort()) {
    const row = klein(JSON.parse(fs.readFileSync(path.join(dir, teil, slug, 'zeile.json'), 'utf8')));
    const r = B.bauen(row, Z, env);
    out[teil + '/' + slug] = r.bundle ? B.json(r.bundle) : 'null';
  }
}
process.stdout.write(JSON.stringify(out));
