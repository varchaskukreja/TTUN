from machina.probe import inspect_bytes

def test_in_memory_probe_extracts_semantics():
    data=b"ISO-10303-21;HEADER;FILE_NAME('part.step','2026',('Alice'),('Acme'),'CAD','CAD','');ENDSEC;DATA;ENDSEC;END-ISO-10303-21;"
    result=inspect_bytes(data,'.step','part.step')
    assert result['status']=='neutral_metadata'
    assert {e['identity'] for e in result['identity_candidates']}=={'Alice','Acme'}

def test_unsupported_probe_never_claims_coverage():
    assert inspect_bytes(b'unknown-format-payload','.sldprt','part.sldprt')['status']=='unsupported'

def test_truncated_native_source_defect():
    result=inspect_bytes(b'X\x00','.sldprt','part.sldprt')
    assert result['status']=='source_defect' and result['candidates']==[]
