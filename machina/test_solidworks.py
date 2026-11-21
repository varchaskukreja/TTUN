import struct
import unittest
import zlib
from machina.solidworks import MARKER, read_streams, rewrite_bytes


def fixture(payload=b'<root><creator>Alice</creator><company>Alice</company></root>', slack=64, name=b'docProps/core.xml'):
    encoded = bytes(((x >> 4) | (x << 4)) & 255 for x in name)
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    compressed = co.compress(payload) + co.flush()
    csz = len(compressed) + slack
    crc = zlib.crc32(payload)
    header = b'ABCD' + MARKER + bytes(4) + struct.pack('<IIII', crc, csz, len(payload), len(name))
    stream = header + encoded + compressed + bytes(slack)
    toc = header + bytes(10) + struct.pack('<I', 0) + encoded
    return b'ABCD\x00\x00\x00\x04' + stream + toc


class SolidWorksTests(unittest.TestCase):
    def test_noop_exact(self):
        data = fixture()
        self.assertEqual(rewrite_bytes(data, {})[0], data)
        self.assertEqual(len(read_streams(data)[0]), 1)

    def test_crc_span_and_metadata(self):
        data = fixture()
        result, report = rewrite_bytes(data, {'Alice': 'Person-Q'})
        self.assertEqual(len(data), len(result))
        self.assertEqual(report['changed_streams'], 1)
        streams, errors = read_streams(result)
        self.assertFalse(errors)
        self.assertNotIn(b'Alice', streams[0].payload)
        self.assertIn(b'Person-Q', streams[0].payload)
        self.assertEqual(streams[0].crc, zlib.crc32(streams[0].payload))
        # Directory CRC is updated too (at the preserved next-stream offset).
        self.assertEqual(struct.unpack_from('<I', result, streams[0].end+14)[0], streams[0].crc)

    def test_casefold_keys_and_identity_boundaries(self):
        data = fixture(b'<root>ALICE / Alice / alice / Malice / Alice2 / Alice_B</root>')
        result, report = rewrite_bytes(data, {'alice': 'Agent'})
        self.assertEqual(read_streams(result)[0][0].payload,
                         b'<root>Agent / Agent / Agent / Malice / Alice2 / Agent_B</root>')
        self.assertEqual(report['changed_streams'], 1)

    def test_conflicting_casefold_mapping(self):
        with self.assertRaisesRegex(ValueError, 'conflicting_casefold'):
            rewrite_bytes(fixture(), {'Alice': 'Agent', 'ALICE': 'Other'})

    def test_xml_reference_attributes_preserve_structural_fields(self):
        xml=b'<root swPath="C:\\Users\\Alice\\Alice_part.sldprt" swName="Alice_part" id="Alice" Material="Alice">Alice</root>'
        result, report = rewrite_bytes(fixture(xml,name=b'swXmlContents/Features'), {'alice':'Agent'})
        payload=read_streams(result)[0][0].payload
        self.assertIn(b'swName="Agent_part"',payload)
        self.assertIn(b'id="Alice"',payload);self.assertIn(b'Material="Alice"',payload)
        self.assertEqual(report['changed_streams'],1)

    def test_keyword_xml_prefix_preserved(self):
        data=fixture(b'\x86<?xml version="1.0"?><Keywords Name="Alice"/>',name=b'swXmlContents/KeyWords')
        result, _=rewrite_bytes(data,{'alice':'Agent'})
        self.assertEqual(read_streams(result)[0][0].payload,b'\x86<?xml version="1.0"?><Keywords Name="Agent"/>')

    def test_binary_reference_only_in_metadata_streams(self):
        from machina.test_native_reference import framed
        payload=framed('C:\\Users\\Alice\\file.sldprt')
        for name,expected in [(b'Header2',1),(b'Contents/Config-0-Partition',0)]:
            data=fixture(payload,name=name)
            result,report=rewrite_bytes(data,{'alice':'Agent'})
            self.assertEqual(report['changed_references'],expected)
            if not expected:self.assertEqual(data,result)

    def test_overflow_refused(self):
        data = fixture(slack=0)
        result, report = rewrite_bytes(data, {'Alice': ''.join(chr(33+i) for i in range(80))})
        self.assertEqual(result, data)
        self.assertIn('compressed_span_overflow', report['issues'])

    def test_bad_crc_rejected(self):
        data = bytearray(fixture()); data[22] ^= 1
        self.assertFalse(read_streams(bytes(data))[0])
        self.assertIn('stream_crc_error', read_streams(bytes(data))[1])

    def test_no_directory_refused(self):
        data = fixture(); streams, _ = read_streams(data)
        data = data[:streams[0].end]
        result, report = rewrite_bytes(data, {'Alice': 'Agent'})
        self.assertEqual(data, result)
        self.assertIn('missing_or_ambiguous_directory_entry', report['issues'])

    def test_structural_xml_keys_untouched(self):
        data = fixture(b'<Alice><creator>Alice</creator></Alice>')
        result, _ = rewrite_bytes(data, {'Alice': 'Agent'})
        self.assertEqual(read_streams(result)[0][0].payload, b'<Alice><creator>Agent</creator></Alice>')

if __name__ == '__main__':
    unittest.main()
