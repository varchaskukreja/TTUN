import json
import pytest
from machina.batch import run_pipeline,plan_inputs
from machina.identity import Registry

KEY=b'private-batch-test-key'

def test_manifest_excludes_operational_files_and_checks_size(tmp_path):
    source=tmp_path/'input';source.mkdir()
    (source/'part.txt').write_text('Acme')
    meta=source/'.get-input';meta.mkdir();(meta/'secret.log').write_text('raw private names')
    manifest=tmp_path/'manifest.tsv';manifest.write_text('part.txt\t4\t\tcorpus/part.txt\n')
    registry=Registry([{'kind':'company','identity':'Acme','pseudonym':'Company-A'}],KEY)
    with pytest.raises(ValueError,match='requires --manifest'):plan_inputs(source,tmp_path/'bad',registry)
    result=run_pipeline(source,tmp_path/'output',registry,tmp_path/'report',manifest=manifest)
    assert result['files']==1 and list((tmp_path/'output').iterdir())==[tmp_path/'output'/'part.txt']
    assert (tmp_path/'output'/'part.txt').read_text()=='Company-A'
    manifest.write_text('part.txt\t99\t\tcorpus/part.txt\n')
    with pytest.raises(ValueError,match='size mismatch'):plan_inputs(source,tmp_path/'out2',registry,manifest)

def test_multiprocess_execution_and_complete_ledger(tmp_path):
    source=tmp_path/'input';source.mkdir()
    for i in range(5):(source/f'{i}.txt').write_text('Acme')
    registry=Registry([{'kind':'company','identity':'Acme','pseudonym':'Company-A'}],KEY)
    result=run_pipeline(source,tmp_path/'output',registry,tmp_path/'report',workers=2)
    assert result['files']==result['expected_files']==5 and result['errors']==0
    rows=[json.loads(l) for l in (tmp_path/'report').read_text().splitlines()]
    assert {r['input'] for r in rows}=={f'{i}.txt' for i in range(5)}
    assert not list((tmp_path/'output').glob('*.machina-partial'))

def test_report_preflight_preserves_existing(tmp_path):
    source=tmp_path/'input';source.mkdir();(source/'x.txt').write_text('x')
    report=tmp_path/'report';report.write_text('existing')
    with pytest.raises(ValueError,match='report must be new'):
        run_pipeline(source,tmp_path/'output',Registry([],KEY),report)
    assert not (tmp_path/'output').exists() and report.read_text()=='existing'
