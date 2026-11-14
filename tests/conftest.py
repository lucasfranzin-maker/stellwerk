import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "beispiele"))
import erzeugen  # noqa: E402

from stellwerk import config  # noqa: E402


@pytest.fixture
def cfg():
    c = config.load.__wrapped__() if hasattr(config.load, "__wrapped__") else __import__("copy").deepcopy(config.DEFAULTS)
    c["arbeitsbereich"] = ""
    c["systeme"] = {"patient": "https://haus.example/sid/pat", "fall": "https://haus.example/sid/fall",
                    "dokument": "https://haus.example/sid/dok"}
    return c


@pytest.fixture(scope="session")
def bsp(tmp_path_factory):
    d = tmp_path_factory.mktemp("beispiele")
    erzeugen.main(d)
    return d


class Api:
    """Testzugang zum HTTP-Dienst. Liefert bei Fehlern ApiError wie die Workbench selbst."""
    def __init__(self, client):
        self.c = client

    def __call__(self, method, path, body=None, query=None, raw=False):
        from stellwerk.workbench import ApiError
        kw = {"params": query or {}}
        if raw:
            kw["content"] = body
        elif body is not None:
            kw["json"] = body
        r = self.c.request(method, "/api/" + path, **kw)
        if r.status_code >= 400:
            raise ApiError(r.status_code, r.json().get("fehler", ""))
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else r.content


@pytest.fixture
def client(cfg, tmp_path):
    from fastapi.testclient import TestClient

    from stellwerk.app import create_app
    cfg["datenbank"] = f"sqlite:///{tmp_path / 'db' / 'stellwerk.db'}"
    cfg["ausgabe"] = str(tmp_path / "out")
    with TestClient(create_app(cfg)) as c:
        yield c


@pytest.fixture
def api(client):
    return Api(client)
