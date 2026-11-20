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

def test_filename_separators_and_unicode_case_equivalence():
    r=Registry([{'kind':'account','identity':'Alice','pseudonym':'User-A'},{'kind':'person','identity':'I','pseudonym':'Person-I'}],KEY)
    assert r.path('sources/github/Alice__project/Alice_part.step')=='sources/github/User-A__project/User-A_part.step'
    assert r.replace('Alice_part.step')=='User-A_part.step'
    assert r.replace('Malice Alice2')=='Malice Alice2'
    # Python IGNORECASE considers this equivalent to I; explicit alias absent.
    assert r.replace('İ')=='İ'

def test_contact_pseudonyms_use_reserved_domain():
    r=Registry([{'kind':'email','identity':'alice@example.test'},{'kind':'domain','identity':'example.test'}],KEY)
    assert r.replace('alice@example.test').endswith('@example.invalid')
    assert r.replace('example.test').endswith('.invalid')
    assert r.replace('alice@example.test')!=r.replace('example.test')
