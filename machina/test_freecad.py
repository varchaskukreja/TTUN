import io
import struct
import unittest
import zipfile
from machina import freecad


class FreeCADTests(unittest.TestCase):
    def test_xml_keys_geometry_references(self):
        original = b'''<?xml version="1.0"?><Document SchemaVersion="4"><Objects><Object name="AcmePart" type="Part::Feature"/></Objects><ObjectData><Object name="AcmePart"><Properties><Property name="Label" type="App::PropertyString"><String value="Acme"/></Property><Property name="Shape" type="Part::PropertyPartShape"><Part file="Acme.brp"/></Property><Property name="Length" type="App::PropertyLength"><Float value="12.5"/></Property><Property name="Ref" type="App::PropertyLink"><Link value="AcmePart"/></Property></Properties></Object></ObjectData></Document>'''
        replace = lambda x: x.replace('Acme', 'Company1').replace('Label', 'BAD').replace('12.5', '99')
        output, report = freecad.transform_xml(original, replace)
        self.assertIn(b'name="Label"', output)
        self.assertIn(b'value="12.5"', output)
        self.assertIn(b'file="Company1.brp"', output)
        self.assertEqual(output.count(b'name="Company1Part"'), 2)
        self.assertIn(b'<Link value="Company1Part"', output)
        self.assertNotIn(b'Acme', output)

    def test_color_array_identity_bytes(self):
        data = struct.pack('<III', 2, 0xff0000ff, 0x12345678)
        out, report = freecad.transform_member('Colors', data, lambda x:'changed', info={'type':'App::PropertyColorList'})
        self.assertEqual(out, data)
        self.assertTrue(report['geometry_bytes_preserved'])
        with self.assertRaises(ValueError):
            freecad.transform_member('Colors', data+b'raw', lambda x:x, info={'type':'App::PropertyColorList'})

    def test_material_texture_paths_length_update(self):
        base = struct.pack('<I4I2f', 1, 1, 2, 3, 4, .2, .3)
        strings = [b'Acme.png', b'/Acme/textures', b'fixed-engineering-uuid']
        data = base+b''.join(struct.pack('<I', len(s))+s for s in strings)
        out, report = freecad.transform_material(data, lambda x:x.replace('Acme', 'Company17'))
        self.assertEqual(out[:len(base)],base)
        self.assertIn(b'Company17.png',out)
        self.assertNotIn(b'Acme',out)
        self.assertIn(b'fixed-engineering-uuid',out)
        self.assertEqual(report['changes'],2)
        self.assertEqual(freecad.transform_material(out,lambda x:x)[0],out)

    def test_brep_unknown_text_rejected(self):
        data=b'\nCASCADE Topology V1, (c) Matra-Datavision\nLocations 0\nTShapes 0\n'
        info={'type':'Part::PropertyPartShape'}
        self.assertEqual(freecad.transform_member('shape.brp',data,lambda x:'x',info=info)[0],data)
        with self.assertRaises(ValueError):
            freecad.transform_member('shape.brp',data+b'PersonalIdentity',lambda x:x,info=info)

    def test_untyped_binary_not_assumed_safe(self):
        _,report=freecad.transform_member('ShapeAppearance',b'opaque',lambda x:x)
        self.assertEqual(report['status'],'unsupported')

    def test_types_come_from_xml(self):
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as z:
            z.writestr('Document.xml','<Document><Properties><Property name="Shape" type="Part::PropertyPartShape"><Part file="shape.brp"/></Property></Properties></Document>')
            z.writestr('GuiDocument.xml','<GuiDocument><Property type="App::PropertyMaterialList"><MaterialList file="Appearance" version="3"/></Property></GuiDocument>')
        types=freecad.member_types(buffer.getvalue())
        self.assertEqual(types['shape.brp']['type'],'Part::PropertyPartShape')
        self.assertEqual(types['Appearance']['version'],'3')

if __name__=='__main__': unittest.main()
