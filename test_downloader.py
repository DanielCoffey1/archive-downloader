import hashlib
import io
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from archive_downloader import download, identifier, local_name, safe_target, Paused, App

PAYLOAD = b'archive-test-data-' * 1000
ENTRY = {'name': 'folder/game.zip', 'size': str(len(PAYLOAD)), 'sha1': hashlib.sha1(PAYLOAD).hexdigest()}

class Response(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(data)
        self.status = status
        self.headers = headers or {'Content-Length': str(len(data))}
        self.url = 'https://archive.org/download/test/folder/game.zip'

class FakeClient:
    def __init__(self, ignore=False, corrupt=False, denied=False):
        self.calls = []; self.ignore = ignore; self.corrupt = corrupt; self.denied = denied
    def open(self, url, headers):
        self.calls.append(headers)
        if self.denied: raise HTTPError(url, 403, 'Denied', {}, None)
        data = b'x' * len(PAYLOAD) if self.corrupt else PAYLOAD
        offset = int(headers.get('Range', 'bytes=0-')[6:-1])
        if offset and not self.ignore:
            return Response(data[offset:], 206, {'Content-Range': f'bytes {offset}-{len(data)-1}/{len(data)}', 'Content-Length': str(len(data)-offset)})
        return Response(data)

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name); self.stop = threading.Event()
    def run_download(self, client, attempts=1):
        return download(client, 'test', ENTRY, self.folder, self.stop, lambda *_: None, attempts)
    def partial(self):
        p = self.folder / 'test/.ia-downloader' / (hashlib.sha256(ENTRY['name'].encode()).hexdigest() + '.part')
        p.parent.mkdir(parents=True); p.write_bytes(PAYLOAD[:321]); return p
    def test_full_and_skip(self):
        c = FakeClient(); self.assertEqual(self.run_download(c), 'Complete')
        self.assertEqual(self.run_download(c), 'Already complete'); self.assertEqual(len(c.calls), 1)
    def test_resume(self):
        self.partial(); c = FakeClient(); self.run_download(c)
        self.assertEqual(c.calls[0]['Range'], 'bytes=321-')
        self.assertEqual((self.folder/'test/folder/game.zip').read_bytes(), PAYLOAD)
    def test_ignored_range_restarts(self):
        self.partial(); self.run_download(FakeClient(ignore=True))
        self.assertEqual((self.folder/'test/folder/game.zip').read_bytes(), PAYLOAD)
    def test_corrupt_file_not_published(self):
        with self.assertRaises(OSError): self.run_download(FakeClient(corrupt=True))
        self.assertFalse((self.folder/'test/folder/game.zip').exists())
    def test_pause_preserves_partial(self):
        p = self.partial(); self.stop.set()
        with self.assertRaises(Paused): self.run_download(FakeClient())
        self.assertEqual(p.stat().st_size, 321)
    def test_permission_failure(self):
        with self.assertRaisesRegex(ValueError, 'permission denied'): self.run_download(FakeClient(denied=True))
    def test_existing_wrong_file_preserved(self):
        p = self.folder/'test/folder/game.zip'; p.parent.mkdir(parents=True); p.write_bytes(b'keep')
        with self.assertRaises(ValueError): self.run_download(FakeClient())
        self.assertEqual(p.read_bytes(), b'keep')
    def test_paths_and_identifiers(self):
        for name in ('../bad', '/absolute', 'a/../../x', 'a\\b', 'a//b'):
            with self.assertRaises(ValueError): local_name(name)
        self.assertEqual(identifier('https://archive.org/download/test/a.zip'), 'test')
        with self.assertRaises(ValueError): identifier('https://evil.example/details/test')
        self.assertEqual(str(local_name('CON.zip')), '~reserved~CON.zip')
        self.assertTrue(safe_target(self.folder, 'a:b.zip').is_relative_to(self.folder))

if __name__ == '__main__': unittest.main()
