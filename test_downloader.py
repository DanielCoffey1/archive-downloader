import hashlib
import io
from pathlib import Path
import tempfile
import threading
import unittest
import tkinter as tk
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

class QueueRemovalTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.close_root)
        self.app = App(self.root)
        self.app.entries = [dict(name=name, size='10', queued=True, status='Queued')
                            for name in ('a.zip', 'notes.txt', 'b.zip', 'c.zip')]
        self.app.render()

    def close_root(self):
        self.root.update_idletasks()
        for callback in self.root.tk.call('after', 'info'):
            self.root.after_cancel(callback)
        self.root.destroy()

    def test_remove_filtered_nonadjacent_rows_and_save(self):
        self.app.filter.set('.zip')
        self.app.tree.selection_set('0', '3')
        self.app.remove_selected()
        self.assertEqual([e['name'] for e in self.app.entries], ['notes.txt', 'b.zip'])
        self.assertEqual(self.app.tree.get_children(), ('1',))
        self.assertIn('2 queued / 2 files', self.app.count['text'])
        with tempfile.TemporaryDirectory() as folder:
            import json
            self.app.folder.set(folder)
            self.app.item = 'test'
            self.app.save()
            saved = json.loads((Path(folder) / 'test/.ia-downloader/queue.json').read_text())
            self.assertEqual([e['name'] for e in saved['files']], ['notes.txt', 'b.zip'])

    def test_busy_and_empty_selection_do_not_remove(self):
        self.app.remove_selected()
        self.assertEqual(len(self.app.entries), 4)
        self.app.tree.selection_set('0')
        self.app.set_busy(True)
        self.app.remove_selected()
        self.assertEqual(len(self.app.entries), 4)
        self.assertTrue(self.app.removebutton.instate(['disabled']))
        self.app.set_busy(False)
        self.assertTrue(self.app.removebutton.instate(['!disabled']))

    def test_delete_shortcut_can_remove_all_rows(self):
        self.root.deiconify()
        self.root.update()
        self.app.tree.selection_set(*self.app.tree.get_children())
        self.app.tree.focus_force()
        self.app.tree.event_generate('<Delete>')
        self.root.update()
        self.assertEqual(self.app.entries, [])
        self.assertEqual(self.app.tree.get_children(), ())
        self.assertTrue(self.app.removebutton.instate(['disabled']))


if __name__ == '__main__': unittest.main()
