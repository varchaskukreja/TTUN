import struct
import unittest
from machina.native import property_strings, rewrite_property_stream


def fixture(text='Alice', kind=31, pid=4):
    payload = (text + '\0').encode('utf-16-le' if kind == 31 else 'cp1252')
    value = struct.pack('<II', kind, len(payload) // (2 if kind == 31 else 1)) + payload
    section = struct.pack('<IIII', 16 + len(value), 1, pid, 16) + value
    header = b'\xfe\xff' + bytes(22) + struct.pack('<I', 1) + bytes(16) + struct.pack('<I', 48)
    return header + section


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
