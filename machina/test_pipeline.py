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


def _freecad_fixture():
    import io, zipfile
    geometry=b'\nCASCADE Topology V1, (c) Matra-Datavision\nLocations 0\nTShapes 0\n'
    xml=b'''<?xml version="1.0"?><Document SchemaVersion="4"><Properties><Property name="Company" type="App::PropertyString"><String value="Acme"/></Property></Properties><Objects><Object name="Part" type="Part::Feature"/></Objects><ObjectData><Object name="Part"><Properties><Property name="Shape" type="Part::PropertyPartShape"><Part file="Acme.brp"/></Property></Properties></Object></ObjectData></Document>'''
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as archive:
        archive.writestr('Document.xml',xml)
        archive.writestr('Acme.brp',geometry)
    return output.getvalue(),geometry


def test_freecad_context_updates_refs_and_preserves_geometry():
    import io,zipfile
    from machina.pipeline import transform
    data,geometry=_freecad_fixture()
    registry=Registry([
        {'identity':'Acme','kind':'company','pseudonym':'Company-A'},
        {'identity':'Document','kind':'project','pseudonym':'Project-B'},
    ],KEY)
    output,report=transform(data,'.fcstd',registry)
    with zipfile.ZipFile(io.BytesIO(output)) as archive:
        assert archive.namelist()==['Document.xml','Company-A.brp']
        assert archive.read('Company-A.brp')==geometry
        assert b'file="Company-A.brp"' in archive.read('Document.xml')
        assert b'value="Company-A"' in archive.read('Document.xml')
    assert report['total_members']==2


def test_nested_freecad_context_and_directory_handling():
    import io,zipfile
    from machina.pipeline import transform
    native,geometry=_freecad_fixture()
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w') as archive:
        archive.writestr('Acme/',b'')
        archive.writestr('Acme/part.FCStd',native)
        archive.writestr('Acme/note.txt',b'Acme')
    registry=Registry([{'identity':'Acme','kind':'company','pseudonym':'Company-A'}],KEY)
    output,report=transform(buffer.getvalue(),'.zip',registry)
    assert report['total_members']==5
    with zipfile.ZipFile(io.BytesIO(output)) as outer:
        assert outer.getinfo('Company-A/').is_dir()
        assert outer.read('Company-A/note.txt')==b'Company-A'
        with zipfile.ZipFile(io.BytesIO(outer.read('Company-A/part.FCStd'))) as inner:
            assert inner.read('Company-A.brp')==geometry
            assert b'file="Company-A.brp"' in inner.read('Document.xml')
