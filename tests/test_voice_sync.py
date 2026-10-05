import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('voice_sync', Path(__file__).resolve().parents[1] / 'tools/voice_sync.py')
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)
ID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'


def archive(name, data=b'test', kind=tarfile.REGTYPE):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w:gz') as tar:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.size = len(data) if kind == tarfile.REGTYPE else 0
        tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


class SyncTests(unittest.TestCase):
    def test_valid_member_idempotent_and_private(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            blob = archive('samples/' + ID + '.json')
            sync.unpack(blob, root)
            sync.unpack(blob, root)
            self.assertEqual((root / (ID + '.json')).read_bytes(), b'test')
            self.assertEqual((root / (ID + '.json')).stat().st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                sync.unpack(archive('samples/' + ID + '.json', b'changed'), root)

    def test_unsafe_paths_links_and_temporary_files_refused(self):
        for name, kind in [('samples/../../bad', tarfile.REGTYPE),
                           ('/tmp/bad', tarfile.REGTYPE),
                           ('samples/' + ID + '.json', tarfile.SYMTYPE),
                           ('samples/' + ID + '.json.tmp', tarfile.REGTYPE)]:
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(ValueError):
                    sync.unpack(archive(name, kind=kind), Path(temp))


if __name__ == '__main__':
    unittest.main()
