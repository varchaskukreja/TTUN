import pytest
from machina.identity import Registry

KEY = b'synthetic-test-key-only'
def test_alias_consistency_and_boundaries():
    r = Registry([{'kind':'company','identity':'Example Works','aliases':['EXW']}], KEY)
    expected = r.replace('Example Works')
    assert r.replace('EXW') == expected
    assert r.replace('EXWorks') == 'EXWorks'
    assert r.path('Example Works/EXW.step') == f'{expected}/{expected}.step'

def test_no_cascading_and_distinct_entities():
    r = Registry([{'kind':'person','identity':'Ada','pseudonym':'Bea'}, {'kind':'person','identity':'Bea','pseudonym':'Cee'}], KEY)
    assert r.replace('Ada Bea') == 'Bea Cee'

def test_reject_alias_conflict_and_path_escape():
    with pytest.raises(ValueError):
        Registry([{'kind':'person','identity':'A','aliases':['B']},{'kind':'person','identity':'B'}], KEY)
    with pytest.raises(ValueError): Registry([], KEY).path('../secret.step')
