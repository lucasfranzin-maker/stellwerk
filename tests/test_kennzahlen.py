"""Kennzahlen aus dem Laufprotokoll: je Kanal und Quelle, nur Metadaten."""
from datetime import datetime, timedelta, timezone

import pytest

from stellwerk.db import Store

JETZT = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    s = Store(f"sqlite:///{tmp_path / 'k.db'}")
    s.migrate()
    return s


def lauf(store, vor_h, kanal="convert", quelle="ORBIS", status="ok", dauer=100, fehler=0, pruefen=0, codes=()):
    store.add_run(zeit=JETZT - timedelta(hours=vor_h), kanal=kanal, quelle=quelle, art="hl7", status=status, fehler=fehler,
                  pruefen=pruefen, dauer_ms=dauer, codes=list(codes))


def test_kennzahlen_je_kanal_und_quelle(store):
    for d in (10, 20, 30, 40, 1000):
        lauf(store, 1, dauer=d)
    lauf(store, 2, status="pruefen", pruefen=2, codes=["HL7_ZEITZONE", "ABS_UNBEKANNT"])
    lauf(store, 3, status="pruefen", pruefen=1, codes=["HL7_ZEITZONE"])
    lauf(store, 4, status="fehler", fehler=1, dauer=5, codes=["ENC_UNLESBAR"])
    lauf(store, 5, kanal="mllp", quelle="LIS")
    lauf(store, 30)                                                              # außerhalb von 24 Stunden
    k = store.kennzahlen(24, JETZT)
    assert k["gesamt"] == {"laeufe": 9, "fehler": 1, "pruefen": 2}
    g = {(x["kanal"], x["quelle"]): x for x in k["gruppen"]}
    o = g[("convert", "ORBIS")]
    assert (o["laeufe"], o["ok"], o["pruefen"], o["fehler"]) == (8, 5, 2, 1)
    assert o["dauer_median_ms"] == 40 and o["dauer_p95_ms"] == 1000 and list(o["codes"][0]) == ["HL7_ZEITZONE", 2]
    assert o["letzter"].startswith("2026-11-18T11:00") and g[("mllp", "LIS")]["laeufe"] == 1
    assert store.kennzahlen(48, JETZT)["gesamt"]["laeufe"] == 10


def test_api_liefert_nur_metadaten(client):
    r = client.get("/api/kennzahlen?stunden=999999")
    assert r.status_code == 200 and r.json()["stunden"] == 24 * 90 and set(r.json()) == {"stunden", "seit", "gruppen", "gesamt"}
    assert client.get("/api/kennzahlen?stunden=0").json()["stunden"] == 1
