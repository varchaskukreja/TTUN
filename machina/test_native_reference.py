import struct
import unittest
from machina.native_reference import cstrings, reference_strings, rewrite_reference_strings, text_candidates


def framed(value):
    raw=value.encode('utf-16-le'); count=len(raw)//2
    length=bytes([count]) if count<255 else b'\xff'+struct.pack('<H',count)
    return b'\xff\xfe\xff'+length+raw


class NativeReferenceTests(unittest.TestCase):
    def test_long_cstring_and_surrogates(self):
        value='C:\\Users\\Alice\\'+'z'*280+'😀.sldprt'
        field=list(cstrings(framed(value)))[0]
        self.assertEqual(field.text,value)
        self.assertEqual(field.size,len(value.encode('utf-16-le')))

    def test_equal_length_path_edit_preserves_framing(self):
        data=b'PREFIX'+framed('C:\\Users\\ALICE\\Alice_part.sldprt')+b'TAIL'
        result, report=rewrite_reference_strings(data,{'alice':'Agent'})
        self.assertEqual(len(result),len(data))
        self.assertEqual(report['changed_references'],1)
        self.assertEqual(reference_strings(result)[0].text,'C:\\Users\\Agent\\Agent_part.sldprt')
        self.assertEqual(result[:10],data[:10]);self.assertTrue(result.endswith(b'TAIL'))

    def test_geometry_and_unframed_strings_untouched(self):
        data=framed('Alice')+'C:\\Users\\Alice\\file.sldprt'.encode('utf-16-le')
        self.assertEqual(rewrite_reference_strings(data,{'alice':'Agent'})[0],data)

    def test_growth_and_truncation_refused(self):
        data=framed('Alice.sldprt')
        result, report=rewrite_reference_strings(data,{'alice':'Person-123'})
        self.assertEqual(result,data);self.assertEqual(report['rejected_references'],1)
        self.assertFalse(list(cstrings(data[:-2])))

    def test_readonly_candidates_include_unframed(self):
        data=b'author@example.invalid'+bytes(3)+'C:\\Users\\Alice\\x.ipt'.encode('utf-16-le')
        candidates=list(text_candidates(data))
        self.assertTrue(any('example.invalid' in f.text for f in candidates))
        self.assertTrue(any('Alice' in f.text for f in candidates))

if __name__=='__main__':unittest.main()
