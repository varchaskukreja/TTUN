from machina.discovery import contacts,step_metadata,xml_metadata,provenance,scan_candidates

def nonempty(items):return [i for i in items if i]

def test_step_roles_not_geometry_or_software():
    source=b"ISO-10303-21;HEADER;FILE_NAME('/Users/alice/part.step','2026',('Alice O''Brien'),('Acme'),'CAD Application','CAD','');ENDSEC;DATA;#1=PRODUCT('steel','6061','material',());ENDSEC;"
    entries=nonempty(step_metadata(source,'part.step'))
    assert {(e['identity'],e['kind']) for e in entries}=={('alice','account'),("Alice O'Brien",'person'),('Acme','company')}

def test_contacts_and_system_accounts():
    entries=nonempty(contacts('alice@example.test C:\\Users\\alice\\Drawing.step /Users/Public/x https://example.test/docs','x'))
    assert {e['identity'] for e in entries}=={'alice@example.test','alice','example.test'}

def test_xml_roles_and_provenance():
    entries=nonempty(xml_metadata('<root><creator>Alice</creator><material>Steel</material></root>','x'))
    assert len(entries)==1 and entries[0]['kind']=='person'
    entries=nonempty(provenance('sources/github/owner__gearbox/model.step'))
    assert [e['kind'] for e in entries]==['account','project']

def test_native_material_not_classified():
    record={'path':'x.ipt','native':{'properties':[{'stream':['\x05SummaryInformation'],'property_id':4,'text':'Alice'},{'stream':['Custom'],'property_id':4,'text':'Aluminum 6061'}]}}
    assert [e['identity'] for e in nonempty(scan_candidates(record))]==['Alice']
