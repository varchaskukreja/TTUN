import ast
import json
import unittest
from machina import source_formats as source


def replace(value):
    return value.replace('Acme', 'Company_17')


class SourceTests(unittest.TestCase):
    def test_python_comments_strings_and_numeric_code(self):
        data=b'''# Acme design\nimport math\nlabel = r"Acme\\part"\ndef volume(r):\n    """Acme helper."""\n    return (4/3)*math.pi*r**3\n'''
        output,report=source.transform(data,'.py',replace)
        self.assertNotIn(b'Acme',output)
        self.assertIn(b'(4/3)*math.pi*r**3',output)
        self.assertIn(b'import math',output)
        namespace={};exec(output,namespace)
        self.assertEqual(namespace['label'],'Company_17\\part')
        self.assertEqual(report['status'],'processed')

    def test_python_fstring_and_identifier_fail_closed(self):
        for data in (b'name=f"Acme {1+2}"\n',b'Acme=123\n',b'name=b"Acme"\n'):
            output,report=source.transform(data,'.py',replace)
            self.assertEqual(report['status'],'unsupported')
            self.assertIn(b'Acme',output)

    def test_python_encoding_and_multiline(self):
        data='# coding: latin-1\nname="Acme café"\n'.encode('latin-1')
        output,report=source.transform(data,'.py',replace)
        self.assertIn('Company_17 café',output.decode('latin-1'))
        ast.parse(output)

    def test_json_values_numeric_and_keys(self):
        data=b'{"name":"Acme", "dimensions": [1, 2.50, -3], "ref":"Acme/model.step"}'
        output,report=source.transform(data,'.json',replace)
        self.assertEqual(json.loads(output)['name'],'Company_17')
        self.assertIn(b'[1, 2.50, -3]',output)
        self.assertEqual(report['status'],'processed')
        _,report=source.transform(b'{"Acme":"part"}', '.json',replace)
        self.assertEqual(report['status'],'unsupported')

    def test_scad_include_comments_strings_geometry(self):
        data=b'''// Acme machine\ninclude <Acme/parts.scad>\nuse <lib/Acme.scad>\n/* Acme */\nlabel="Acme\\npart";\nmodule part() { cube([12, 20, 3]); }\npart();\n'''
        output,report=source.transform(data,'.scad',replace)
        self.assertEqual(report['status'],'processed')
        self.assertNotIn(b'Acme',output)
        self.assertIn(b'include <Company_17/parts.scad>',output)
        self.assertIn(b'cube([12, 20, 3]);',output)
        self.assertIn(b'label="Company_17\\npart";',output)

    def test_scad_comment_tokens_in_strings(self):
        data=b'url="https://Acme.test/*x*/"; cube(2);'
        output,report=source.transform(data,'.scad',replace)
        self.assertEqual(output,b'url="https://Company_17.test/*x*/"; cube(2);')

    def test_noop_exact_bytes(self):
        for suffix,data in [('.scad',b'cube(2); // public\r\n'),('.py',b'x = "unchanged"\r\n'),('.json',b'{ "units" : "mm" }')]:
            self.assertEqual(source.transform(data,suffix,lambda s:s)[0],data)

    def test_scad_unknown_escape_rejected(self):
        _,report=source.transform(b'path="Acme\\q";', '.scad',replace)
        self.assertEqual(report['status'],'unsupported')


if __name__=='__main__':unittest.main()
