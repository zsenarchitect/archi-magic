import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from batch_family_type_table import bind_table
from family_library_discovery import discover, manifest_digest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Use the ordinary POSIX staging fixture.")


@pytest.fixture
def discovery(tmp_path):
    root = tmp_path.resolve() / "library"
    root.mkdir()
    (root / "A").mkdir()
    (root / "A" / "Fixture.rfa").write_bytes(b"reviewed family")
    (root / "A" / "Fixture.txt").write_bytes(b"reviewed catalog")
    (root / "B.rfa").write_bytes(b"another family")
    return discover(str(root))


def test_cli_binds_exact_names_units_and_source_pairs(tmp_path, discovery):
    table = (
        'source_path,seed_type,type,Width[mm],Ratio[1]\r\n'
        'A/Fixture.rfa,"Seed, tall","Wide ""A""",1200.50,.75\r\n'
        'B.rfa,Existing,Existing,450,1\r\n'
    ).encode("utf-8-sig")
    table_path = tmp_path.resolve() / "types.csv"
    manifest = tmp_path.resolve() / "discovery.json"
    table_path.write_bytes(table)
    manifest.write_text(json.dumps(discovery))
    response = subprocess.run([sys.executable, str(SCRIPTS / "batch_family_type_table.py"),
                               "--table", str(table_path), "--manifest", str(manifest)],
                              capture_output=True, text=True, check=True)
    result = json.loads(response.stdout)
    assert result["discovery_sha256"] == discovery["manifest_sha256"]
    assert result["table_sha256"] == hashlib.sha256(table).hexdigest()
    assert result["sources"] == discovery["files"]
    assert result["rows"] == [
        {"line": 2, "source_path": "A/Fixture.rfa", "seed_type": "Seed, tall", "type": 'Wide "A"',
         "sizes": [{"parameter": "Width", "unit": "mm", "value": "1200.50"},
                   {"parameter": "Ratio", "unit": "1", "value": ".75"}]},
        {"line": 3, "source_path": "B.rfa", "seed_type": "Existing", "type": "Existing",
         "sizes": [{"parameter": "Width", "unit": "mm", "value": "450"},
                   {"parameter": "Ratio", "unit": "1", "value": "1"}]},
    ]
    assert result.pop("request_sha256") == manifest_digest(result)


@pytest.mark.parametrize(("table", "reason"), [
    ("source_path,seed_type,type,Width[kg]\nB.rfa,Seed,New,3", "explicit unit"),
    ("source_path,seed_type,type,Width[mm],width[ft]\nB.rfa,Seed,New,3,4", "duplicate name"),
    ("source_path,seed_type,type,Width[mm]\nB.rfa,Seed,New,NaN", "finite decimal"),
    ("source_path,seed_type,type,Width[mm]\nB.rfa,Seed,New,2\nB.rfa,Other,new,3", "repeats"),
    ("source_path,seed_type,type,Width[mm]\nb.rfa,Seed,New,2", "unavailable"),
    ("source_path,seed_type,type,Width[mm]\n../B.rfa,Seed,New,2", "root-relative"),
])
def test_rejects_ambiguous_unbound_or_malformed_rows(discovery, table, reason):
    with pytest.raises(ValueError, match=reason):
        bind_table(table.encode(), discovery)
