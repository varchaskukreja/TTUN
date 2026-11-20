from machina.inventory import rows, sample
import pytest

def test_reject_manifest_traversal(tmp_path):
    p=tmp_path/'manifest.tsv';p.write_text('../escape\t12\t\tcorpus/x\n')
    with pytest.raises(ValueError):list(rows(p))

def test_sampling_bounded_and_reproducible():
    records=[{'path':f'{i}.step','size':10} for i in range(100)] + [{'path':'a.pdf','size':12}]
    selected=sample(records,3,20,35)
    assert sum(r['size'] for r in selected)<=35
    assert selected==sample(list(reversed(records)),3,20,35)
    assert any(r['path'].endswith('.pdf') for r in selected)
