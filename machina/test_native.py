import struct
import unittest
import uuid
from machina.native import property_strings, rewrite_property_stream


def fixture(text='Alice', kind=31, pid=4):
    payload = (text + '\0').encode('utf-16-le' if kind == 31 else 'cp1252')
    value = struct.pack('<II', kind, len(payload) // (2 if kind == 31 else 1)) + payload
    section = struct.pack('<IIII', 16 + len(value), 1, pid, 16) + value
    header = b'\xfe\xff' + bytes(22) + struct.pack('<I', 1) + bytes(16) + struct.pack('<I', 48)
    return header + section


def multi_fixture(values, padding=0, fmtid='f29f85e0-4ff9-1068-ab91-08002b27b3d9'):
    blocks = []
    for pid, text in values:
        raw = (text + '\0').encode('utf-16-le')
        block = struct.pack('<II', 31, len(raw)//2) + raw
        blocks.append((pid, block + bytes((-len(block)) % 4)))
    offset = 8 + 8*len(blocks)
    entries = []
    for pid, block in blocks:
        entries.append(struct.pack('<II', pid, offset))
        offset += len(block)
    section = struct.pack('<II', offset, len(blocks)) + b''.join(entries) + b''.join(b for _, b in blocks)
    return b'\xfe\xff' + bytes(22) + struct.pack('<I', 1) + uuid.UUID(fmtid).bytes_le + struct.pack('<I', 48) + section + bytes(padding)


class NativeTests(unittest.TestCase):
    def test_utf16_and_ansi(self):
        for kind in [30, 31]:
            data = fixture(kind=kind)
            fields, issues = property_strings(data)
            self.assertEqual(fields[0].text, 'Alice')
            self.assertFalse(issues)
            changed, report = rewrite_property_stream(data, {'Alice': 'Agent'})
            self.assertEqual(len(data), len(changed))
            self.assertEqual(property_strings(changed)[0][0].text, 'Agent')
            self.assertEqual(report['changed_fields'], 1)
            self.assertEqual(data[:fields[0].offset], changed[:fields[0].offset])

    def test_casefold_keys_and_identity_boundaries(self):
        data = fixture('ALICE / Alice / alice / Malice / Alice2 / Alice_B')
        out, report = rewrite_property_stream(data, {'alice': 'Agent'})
        self.assertEqual(property_strings(out)[0][0].text,
                         'Agent / Agent / Agent / Malice / Alice2 / Agent_B')
        self.assertEqual(report['changed_fields'], 1)

    def test_conflicting_casefold_mapping(self):
        with self.assertRaisesRegex(ValueError, 'conflicting_casefold'):
            rewrite_property_stream(fixture(), {'Alice': 'Agent', 'ALICE': 'Other'})

    def test_inventor_semantics_exclude_engineering_values(self):
        data = multi_fixture([(41, 'Alice'), (20, 'Aluminum'), (5, 'Part-123')],
                             fmtid='32853f0f-3444-11d1-9e93-0060b03c1ca6')
        fields, _ = property_strings(data)
        self.assertEqual([f.semantic for f in fields], ['designer', None, None])

    def test_grow_into_stream_padding(self):
        data = multi_fixture([(4, 'Al'), (2, 'Engineering Title')], padding=100)
        out, report = rewrite_property_stream(data, {'Al': 'Person-12345'})
        fields, issues = property_strings(out)
        self.assertFalse(issues)
        self.assertEqual([f.text for f in fields], ['Person-12345', 'Engineering Title'])
        self.assertEqual(report['changed_fields'], 1)
        self.assertEqual(len(data), len(out))
        self.assertEqual(fields[0].semantic, 'author')
        self.assertIsNone(fields[1].semantic)

    def test_shrink_funds_growth_without_stream_resize(self):
        data = multi_fixture([(4, 'Long Identifying Person Name'), (8, 'Al'), (2, 'Title')])
        out, report = rewrite_property_stream(data, {'Long Identifying Person Name': 'Person-1', 'Al': 'Person-12345'})
        self.assertEqual([f.text for f in property_strings(out)[0]], ['Person-1', 'Person-12345', 'Title'])
        self.assertEqual(report['changed_fields'], 2)
        self.assertEqual(len(data), len(out))

    def test_nonzero_external_padding_not_reclaimed(self):
        data = multi_fixture([(4, 'Al')]) + b'KEEP THIS DATA'
        out, report = rewrite_property_stream(data, {'Al': 'Person-12345'})
        self.assertEqual(data, out)
        self.assertEqual(report['rejected_fields'], 1)

    def test_shorter_value_scrubs_original_tail(self):
        data = multi_fixture([(4, 'Long Identifying Person Name'), (2, 'Engineering Title')])
        out, report = rewrite_property_stream(data, {'Long Identifying Person Name': 'Person-1'})
        self.assertEqual(property_strings(out)[0][0].text, 'Person-1')
        self.assertNotIn('Identifying'.encode('utf-16-le'), out)
        self.assertEqual(report['changed_fields'], 1)

    def test_refuses_length_change(self):
        data = fixture()
        out, report = rewrite_property_stream(data, {'Alice': 'Person-123'})
        self.assertEqual(out, data)
        self.assertEqual(report['rejected_fields'], 1)

    def test_no_cascading(self):
        out, _ = rewrite_property_stream(fixture(), {'Alice': 'Agent', 'Agent': 'Other'})
        self.assertEqual(property_strings(out)[0][0].text, 'Agent')

    def test_structural_keys_untouched(self):
        for pid in (0, 1, 255):
            data = fixture(pid=pid)
            self.assertEqual(rewrite_property_stream(data, {'Alice': 'Agent'})[0], data)

    def test_corrupt_lengths(self):
        data = bytearray(fixture())
        struct.pack_into('<I', data, 68, 100000)
        fields, issues = property_strings(data)
        self.assertFalse(fields)
        self.assertIn('invalid_string_length', issues)

    def test_truncation_and_random_data(self):
        for data in [b'', bytes(500), fixture()[:-1]]:
            fields, issues = property_strings(data)
            self.assertFalse(fields)
            self.assertTrue(issues)


if __name__ == '__main__':
    unittest.main()
