import gzip
import io
import stat
import tarfile
import unittest
import zipfile

from machina.archives import ArchiveError, Limits, kind, transform


def make_zip(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


def callback(name, data):
    return (name.replace('Acme', 'Company_1'), data if kind(name, data) else data.replace(b'Acme', b'Company_1'), {'status': 'processed'})


class ArchiveTests(unittest.TestCase):
    def test_nested_zip_and_references(self):
        source = make_zip([('Acme/assembly.txt', b'ref Acme/model.txt'), ('Acme/inner.zip', make_zip([('Acme/model.txt', b'Acme model')]))])
        output, report = transform(source, 'root.zip', callback)
        with zipfile.ZipFile(io.BytesIO(output)) as outer:
            self.assertEqual(outer.read('Company_1/assembly.txt'), b'ref Company_1/model.txt')
            with zipfile.ZipFile(io.BytesIO(outer.read('Company_1/inner.zip'))) as inner:
                self.assertEqual(inner.read('Company_1/model.txt'), b'Company_1 model')
        self.assertEqual(report['total_members'], 3)

    def test_traversal_and_backslash(self):
        for name in ('../escape', '/absolute', 'C:/drive', 'a/../escape', 'a\\escape'):
            with self.subTest(name=name), self.assertRaises(ArchiveError):
                transform(make_zip([(name, b'x')]), 'root.zip', callback)

    def test_output_collision(self):
        source = make_zip([('Acme.txt', b'1'), ('Company_1.txt', b'2')])
        with self.assertRaises(ArchiveError):
            transform(source, 'root.zip', callback)

    def test_symlink(self):
        info = zipfile.ZipInfo('link')
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(ArchiveError):
            transform(make_zip([(info, b'target')]), 'root.zip', callback)

    def test_expansion_limit(self):
        source = make_zip([('a.txt', b'x' * 1000)])
        with self.assertRaises(ArchiveError):
            transform(source, 'root.zip', callback, limits=Limits(max_expanded_bytes=100))

    def test_depth_limit(self):
        source = make_zip([('inner.zip', make_zip([('a.txt', b'x')]))])
        with self.assertRaises(ArchiveError):
            transform(source, 'root.zip', callback, limits=Limits(max_depth=0))

    def test_unknown_member_fails_closed(self):
        with self.assertRaises(ArchiveError):
            transform(make_zip([('a.bin', b'x')]), 'root.zip', lambda n, d: (n, d, {'status': 'unsupported'}))

    def test_targz_metadata(self):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w') as archive:
            info = tarfile.TarInfo('Acme.txt'); info.uname = 'Acme'; info.size = 4
            archive.addfile(info, io.BytesIO(b'Acme'))
        output, report = transform(gzip.compress(buffer.getvalue()), 'Acme.tar.gz', callback)
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(output)), mode='r:') as archive:
            info = archive.getmember('Company_1.txt')
            self.assertEqual(info.uname, 'Company_1')
            self.assertEqual(archive.extractfile(info).read(), b'Company_1')

    def test_zip_comments(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            info = zipfile.ZipInfo('Acme.txt'); info.comment = b'Acme'
            archive.writestr(info, b'Acme'); archive.comment = b'Acme'
        output, _ = transform(buffer.getvalue(), 'root.zip', callback)
        with zipfile.ZipFile(io.BytesIO(output)) as archive:
            self.assertEqual(archive.comment, b'Company_1')
            self.assertEqual(archive.getinfo('Company_1.txt').comment, b'Company_1')

    def test_directory_preserved(self):
        output, _ = transform(make_zip([('Acme/', b''), ('Acme/a.txt', b'Acme')]), 'root.zip', callback)
        with zipfile.ZipFile(io.BytesIO(output)) as archive:
            self.assertTrue(archive.getinfo('Company_1/').is_dir())


if __name__ == '__main__':
    unittest.main()
