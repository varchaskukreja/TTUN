import json
import pytest
from pathlib import Path
from machina.identity import Registry
from machina.pipeline import run

KEY=b'private-test-key-123'
def test_paths_metadata_geometry_and_ledger(tmp_path):
    source=tmp_path/'input';source.mkdir()
    data=b"ISO-10303-21;HEADER;FILE_NAME('Acme','2026',('Acme'),('Acme'),'CAD','CAD','');ENDSEC;DATA;#1=CARTESIAN_POINT('',(1.,2.,3.));ENDSEC;END-ISO-10303-21;"
    (source/'Acme.step').write_bytes(data)
    registry=Registry([{'identity':'Acme','kind':'company','pseudonym':'Company-A'}],KEY)
    result=run(source,tmp_path/'output',registry,tmp_path/'private.jsonl')
    output=(tmp_path/'output'/'Company-A.step').read_bytes()
    assert b'Acme' not in output
    assert output.split(b'DATA;')[1]==data.split(b'DATA;')[1]
    assert result['files']==1 and not result['ready_to_submit']
    assert json.loads((tmp_path/'private.jsonl').read_text())['output']=='Company-A.step'

def test_collision_preflight_writes_nothing(tmp_path):
    source=tmp_path/'input';source.mkdir()
    (source/'Acme.step').touch();(source/'Company-A.step').touch()
    registry=Registry([{'identity':'Acme','kind':'company','pseudonym':'Company-A'}],KEY)
    with pytest.raises(ValueError):run(source,tmp_path/'output',registry,tmp_path/'report')
    assert not (tmp_path/'output').exists()
