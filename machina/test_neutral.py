import unittest
from machina.neutral import extract, transform


class NeutralTests(unittest.TestCase):
    def test_step_nested_quotes_and_geometry(self):
        original = b"""ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('Acme; design'),'2;1');
FILE_NAME('Acme.step','2026-11-20',('O''Brien'),('Acme'),'CAD v1','CAD','Acme');
FILE_SCHEMA(('Acme'));
ENDSEC;
DATA;
#1=PRODUCT('Acme','Acme','Acme',(#2));
#2=CARTESIAN_POINT('Acme',(1.234,-5.,0.));
/* PRODUCT('Acme') */
ENDSEC;
END-ISO-10303-21;
"""
        out, report = transform(original, '.step', lambda x: x.replace('Acme', 'Company_17').replace("O'Brien", "Person_2"))
        self.assertIn(b"FILE_SCHEMA(('Acme'))", out)
        self.assertIn(b"CARTESIAN_POINT('Acme',(1.234,-5.,0.))", out)
        self.assertIn(b"/* PRODUCT('Acme') */", out)
        self.assertIn(b"('Person_2')", out)
        self.assertIn(b"'2026-11-20'", out)
        self.assertNotIn('CAD v1', extract(original, '.step'))
        self.assertEqual(report['changes'], 8)

    def test_step_replacement_quote_escaping(self):
        source = b"ISO-10303-21; HEADER; FILE_NAME('Name','',(''),(''),'','',''); ENDSEC;"
        out, _ = transform(source, '.stp', lambda x: "D'Angelo" if x == 'Name' else x)
        self.assertIn(b"'D''Angelo'", out)

    def test_stl_geometry_unchanged(self):
        source = b'solid Acme\nfacet normal 0 0 1\nouter loop\nvertex 1 2 3\nendloop\nendfacet\nendsolid Acme\n'
        out, _ = transform(source, '.stl', lambda s: s.replace('Acme', 'Part_12'))
        self.assertEqual(out, source.replace(b'Acme', b'Part_12'))

    def test_binary_unsupported(self):
        data = b'solid\0binary'
        out, report = transform(data, '.stl', lambda s: 'x')
        self.assertEqual(out, data)
        self.assertEqual(report['status'], 'unsupported')

    def test_obj_reference_names(self):
        source = b'mtllib Acme.mtl\no Acme\nusemtl Acme\nv 1 2 3\nf 1 2 3\n'
        out, _ = transform(source, '.obj', lambda s: s.replace('Acme', 'Part_1'))
        self.assertEqual(out, source.replace(b'Acme', b'Part_1'))

    def test_svg_preserves_geometry_and_escapes(self):
        source = b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M 1 2 L 3 4"/><text>Acme &amp; Co<tspan>Acme</tspan></text><metadata><author>Acme</author></metadata></svg>'
        out, _ = transform(source, '.svg', lambda s: s.replace('Acme', 'Company <1>'))
        self.assertIn(b'<path d="M 1 2 L 3 4"/>', out)
        self.assertIn(b'Company &lt;1&gt; &amp; Co', out)
        self.assertNotIn(b'Acme', out)

    def test_dxf_geometry_and_text(self):
        source = b'0\r\nSECTION\r\n2\r\nENTITIES\r\n0\r\nTEXT\r\n1\r\nAcme\r\n10\r\n1.23\r\n0\r\nLINE\r\n10\r\n123\r\n0\r\nEOF\r\n'
        out, _ = transform(source, '.dxf', lambda s: s.replace('Acme', 'Company_1'))
        self.assertEqual(out, source.replace(b'Acme', b'Company_1'))

    def test_sat_length_prefix_and_geometry(self):
        source = b'700 0 1 0\n4 Acme 3 1.0 24 Thu Jul  2 12:47:17 2026\n1 0.000001 0.0000000001\nbody $1 -1 $-1 $2 $-1 $-1 #\n'
        out, report = transform(source, '.sat', lambda s: s.replace('Acme', 'Company_17'))
        self.assertEqual(out, source.replace(b'4 Acme', b'10 Company_17'))
        self.assertEqual(report['changes'], 1)

    def test_unchanged_bytes(self):
        source = b'<svg><text>Steel 304, 2 mm</text></svg>'
        self.assertEqual(transform(source, '.svg', lambda s: s)[0], source)


if __name__ == '__main__':
    unittest.main()

def test_pat_geometry_preserved():
    data=b'; Acme patterns\n*Acme, Hatch label\n45,0,0,0,1.0\n-45,0,0,0,1.0\n'
    out,report=transform(data,'.pat',lambda x:x.replace('Acme','Company-A'))
    assert out.endswith(b'45,0,0,0,1.0\n-45,0,0,0,1.0\n')
    assert b'Acme' not in out and report['changes']==2

def test_binary_stl_header_only_and_capacity():
    import struct
    triangle=struct.pack('<12fH',*[float(i) for i in range(12)],123)
    data=b'Acme design'.ljust(80,b'\0')+struct.pack('<I',1)+triangle
    output,report=transform(data,'.stl',lambda x:x.replace('Acme','Company-A'))
    assert output[80:]==data[80:] and b'Acme' not in output[:80]
    assert extract(output,'.stl')==['Company-A design']
    output,report=transform(data,'.stl',lambda x:'X'*81)
    assert output==data and report['status']=='unsupported'

def test_standard_svg_doctype_but_no_entity_expansion():
    data=b'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd"><svg xmlns="http://www.w3.org/2000/svg"><text>Acme</text></svg>'
    out,report=transform(data,'.svg',lambda x:x.replace('Acme','Company-A'))
    assert b'Company-A' in out and report['changes']==1
    hostile=b'<!DOCTYPE svg [<!ENTITY secret "Acme">]><svg><text>&secret;</text></svg>'
    assert transform(hostile,'.svg',lambda x:'Company-A')[1]['status']=='unsupported'

def test_legacy_sat_without_metadata_header():
    data=b'107 0 1 0\nbody $-1 $1 $-1 $-1 #\nlump $-1 $-1 $2 $0 #\n'
    assert extract(data,'.sat')==[]
    assert transform(data,'.sat',lambda x:'Company-A')[0]==data
