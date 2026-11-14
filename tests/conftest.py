import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "beispiele"))
import erzeugen  # noqa: E402

from stellwerk import config  # noqa: E402


@pytest.fixture
def cfg():
    c = config.load.__wrapped__() if hasattr(config.load, "__wrapped__") else __import__("copy").deepcopy(config.DEFAULTS)
    c["systeme"] = {"patient": "https://haus.example/sid/pat", "fall": "https://haus.example/sid/fall",
                    "dokument": "https://haus.example/sid/dok"}
    return c


@pytest.fixture(scope="session")
def bsp(tmp_path_factory):
    d = tmp_path_factory.mktemp("beispiele")
    erzeugen.main(d)
    return d
