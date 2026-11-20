from machina.text_handler import transform_xml

def test_xml_values_references_and_markup_preserved():
    data=b'''<?xml version="1.0"?><Acme xmlns="urn:cad" owner="Acme" file="Acme.step"><v>Acme &amp; steel</v><![CDATA[Acme]]><!-- Acme --></Acme>'''
    output,report=transform_xml(data,lambda x:x.replace('Acme','Company-A'))
    assert b'<Acme ' in output and b'</Acme>' in output
    assert b'file="Company-A.step"' in output
    assert b'Company-A &amp; steel' in output
    assert report['changes']==5

def test_greater_than_inside_attribute():
    data=b'<doc note="Acme > metal">Acme</doc>'
    out,_=transform_xml(data,lambda x:x.replace('Acme','Company-A'))
    assert out==b'<doc note="Company-A &gt; metal">Company-A</doc>'
