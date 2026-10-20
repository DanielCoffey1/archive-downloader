"""Archive.org individual-file downloader. Python 3.11+, standard library only."""
import hashlib
import http.cookiejar
import http.client
import json
import os
from pathlib import Path
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from urllib.parse import urlparse, unquote, quote, urlencode
from urllib.request import Request, build_opener, HTTPCookieProcessor, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

BASE = 'https://archive.org'


class Paused(Exception):
    pass


def identifier(value):
    value = value.strip()
    if '://' in value:
        u = urlparse(value)
        parts = u.path.strip('/').split('/')
        if u.hostname not in ('archive.org', 'www.archive.org') or len(parts) < 2 or parts[0] not in ('details', 'download'):
            raise ValueError('Paste an Archive.org /details/ or /download/ item URL.')
        value = unquote(parts[1])
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', value):
        raise ValueError('Invalid item identifier.')
    return value


def local_name(name):
    parts = name.split('/')
    if any(p in ('', '.', '..') for p in parts) or '\\' in name:
        raise ValueError('Unsafe filename in item: ' + name)
    result = []
    for p in parts:
        p = ''.join('~%04x' % ord(c) if c in '<>:"|?*~' or ord(c) < 32 else c for c in p)
        while p.endswith((' ', '.')):
            p = p[:-1] + '~%04x' % ord(p[-1])
        if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', p, re.I):
            p = '~reserved~' + p
        result.append(p)
    return Path(*result)


def safe_target(root, name):
    root = Path(root).resolve()
    target = root / local_name(name)
    if not target.resolve().is_relative_to(root):
        raise ValueError('Destination escapes the selected folder.')
    return target


def size_text(n):
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if n < 1024:
            return f'{n:,.1f} {unit}'
        n /= 1024
    return f'{n:,.1f} PiB'


class ArchiveRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        u = urlparse(newurl)
        if u.scheme != 'https' or not (u.hostname == 'archive.org' or (u.hostname or '').endswith('.archive.org')):
            raise ValueError('Refused a redirect outside HTTPS Archive.org.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self):
        self.cookies = http.cookiejar.CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies), ArchiveRedirect())

    def open(self, url, headers=None, data=None):
        h = {'User-Agent': 'ArchiveFileDownloader/1.0', 'Accept-Encoding': 'identity'}
        h.update(headers or {})
        return self.opener.open(Request(url, data=data, headers=h), timeout=30)

    def login(self, email, password):
        data = urlencode({'email': email, 'password': password}).encode()
        with self.open(BASE + '/services/xauthn/?op=login', data=data) as r:
            result = json.load(r)
        if not result.get('success'):
            raise ValueError('Sign-in failed. Check your email/password or sign in on archive.org first.')
        values = result.get('values', {}).get('cookies', {})
        for name in ('logged-in-user', 'logged-in-sig'):
            value = values.get(name)
            if not value:
                raise ValueError('Archive.org did not return an account session.')
            self.cookies.set_cookie(http.cookiejar.Cookie(0, name, value.split(';')[0], None, False,
                '.archive.org', True, True, '/', True, True, None, True, None, None, {}))

    def metadata(self, item):
        with self.open(BASE + '/metadata/' + quote(item, safe='')) as r:
            data = json.load(r)
        if not isinstance(data, dict) or not data.get('files'):
            raise ValueError('No files found. Check the item URL and account access.')
        if data.get('metadata', {}).get('mediatype') == 'collection':
            raise ValueError('This is a collection page. Use the page for a specific item inside it.')
        return data


def check_file(path, entry, stop):
    if not path.is_file():
        return False
    expected = int(entry['size']) if str(entry.get('size', '')).isdigit() else None
    if expected is not None and path.stat().st_size != expected:
        return False
    algorithm = next((a for a in ('sha1', 'md5') if entry.get(a)), None)
    if algorithm:
        h = hashlib.new(algorithm)
        with path.open('rb') as f:
            while chunk := f.read(1024 * 1024):
                if stop.is_set():
                    raise Paused()
                h.update(chunk)
        return h.hexdigest().lower() == entry[algorithm].lower()
    return expected is not None


def download(client, item, entry, folder, stop, progress, attempts=5):
    root = Path(folder) / item
    target = safe_target(root, entry['name'])
    scratch = safe_target(root, '.ia-downloader')
    scratch.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(entry['name'].encode()).hexdigest()
    part = safe_target(scratch, key + '.part')
    target.parent.mkdir(parents=True, exist_ok=True)
    if check_file(target, entry, stop):
        return 'Already complete'
    if target.exists():
        raise ValueError('Existing file differs; move or rename it before retrying: ' + str(target))
    expected = int(entry['size']) if str(entry.get('size', '')).isdigit() else None
    url = BASE + '/download/' + quote(item, safe='') + '/' + quote(entry['name'], safe='/')
    for attempt in range(attempts):
        if stop.is_set():
            raise Paused()
        delay = min(60, 2 ** (attempt + 1))
        try:
            if part.exists() and check_file(part, entry, stop):
                part.replace(target)
                return 'Complete'
            offset = part.stat().st_size if part.exists() else 0
            if expected is not None and offset >= expected:
                part.unlink(missing_ok=True)
                offset = 0
            with client.open(url, {'Range': f'bytes={offset}-'} if offset else {}) as r:
                if r.status == 206:
                    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+|\*)', r.headers.get('Content-Range', ''))
                    if not match or int(match[1]) != offset:
                        raise ValueError('Server returned an invalid resume range.')
                elif r.status == 200:
                    offset = 0  # A server ignoring Range must restart, never append.
                else:
                    raise ValueError('Unexpected download response.')
                if '/account/' in urlparse(r.url).path:
                    raise ValueError('This file requires account access. Sign in and retry.')
                content_type = r.headers.get('Content-Type', '').lower()
                if 'text/html' in content_type and not entry['name'].lower().endswith(('.html', '.htm')):
                    raise ValueError('Archive.org returned a web page instead of this file. Check account access.')
                response_size = r.headers.get('Content-Length')
                received = 0
                with part.open('ab' if offset else 'wb') as out:
                    last = 0
                    while True:
                        if stop.is_set():
                            raise Paused()
                        chunk = r.read(256 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
                        received += len(chunk)
                        now = time.monotonic()
                        if now - last > .2:
                            progress(offset + received, expected)
                            last = now
                if response_size and received != int(response_size):
                    raise OSError('Connection ended before the file finished.')
            if expected is not None and part.stat().st_size != expected:
                raise OSError('Downloaded size does not match the file listing.')
            if (entry.get('sha1') or entry.get('md5')) and not check_file(part, entry, stop):
                part.unlink(missing_ok=True)
                raise OSError('Checksum mismatch; downloading again.')
            if target.exists():
                raise ValueError('A destination file appeared during download; refusing to overwrite it.')
            part.replace(target)
            return 'Complete'
        except HTTPError as e:
            if e.code == 416:
                part.unlink(missing_ok=True)
            elif e.code in (401, 403):
                raise ValueError('Account access required or permission denied. Sign in and retry.') from None
            elif e.code not in (429, 500, 502, 503, 504):
                raise ValueError(f'Archive.org returned HTTP {e.code}.') from None
            if str(e.headers.get('Retry-After', '')).isdigit():
                delay = max(delay, min(300, int(e.headers['Retry-After'])))
            if attempt == attempts - 1:
                raise OSError(f'HTTP {e.code} after {attempts} attempts.') from None
        except (OSError, URLError, http.client.IncompleteRead) as e:
            if attempt == attempts - 1:
                raise OSError(str(e)) from None
        if stop.wait(delay):
            raise Paused()
    raise OSError('Download could not finish.')


class App:
    def __init__(self, root):
        self.root = root
        root.title('Archive Downloader')
        root.geometry('1080x720')
        root.minsize(820, 540)
        self.client = Client()
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.busy = False
        self.entries = []
        self.item = ''
        self.url = tk.StringVar(value='https://archive.org/download/sony_playstation_portable_part1')
        self.folder = tk.StringVar(value=str(Path.home() / 'Downloads' / 'Archive'))
        self.filter = tk.StringVar()
        self.status = tk.StringVar(value='Paste an item URL, then load its files. All files are queued by default.')
        style = ttk.Style()
        style.theme_use('vista' if 'vista' in style.theme_names() else 'clam')
        style.configure('Treeview', rowheight=25)
        box = ttk.Frame(root, padding=20)
        box.pack(fill='both', expand=True)
        ttk.Label(box, text='Archive Downloader', font=('Segoe UI', 22, 'bold')).pack(anchor='w')
        ttk.Label(box, text='Download an entire item, one file at a time. No server-side ZIP required.').pack(anchor='w', pady=(2, 16))
        row = ttk.Frame(box); row.pack(fill='x', pady=4)
        ttk.Label(row, text='Item URL', width=13).pack(side='left')
        self.urlentry = ttk.Entry(row, textvariable=self.url); self.urlentry.pack(side='left', fill='x', expand=True)
        self.loadbutton = ttk.Button(row, text='Load files', command=self.load); self.loadbutton.pack(side='left', padx=(8, 0))
        self.loginbutton = ttk.Button(row, text='Sign in…', command=self.login); self.loginbutton.pack(side='left', padx=(8, 0))
        row = ttk.Frame(box); row.pack(fill='x', pady=4)
        ttk.Label(row, text='Save to', width=13).pack(side='left')
        self.folderentry = ttk.Entry(row, textvariable=self.folder); self.folderentry.pack(side='left', fill='x', expand=True)
        self.browsebutton = ttk.Button(row, text='Browse…', command=self.browse); self.browsebutton.pack(side='left', padx=(8, 0))
        row = ttk.Frame(box); row.pack(fill='x', pady=(14, 8))
        ttk.Label(row, text='Filter filenames', width=16).pack(side='left')
        ttk.Entry(row, textvariable=self.filter).pack(side='left', fill='x', expand=True)
        self.filter.trace_add('write', lambda *_: self.render())
        self.allbutton = ttk.Button(row, text='Queue shown', command=lambda: self.select(True)); self.allbutton.pack(side='left', padx=4)
        self.nonebutton = ttk.Button(row, text='Unqueue shown', command=lambda: self.select(False)); self.nonebutton.pack(side='left')
        area = ttk.Frame(box); area.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(area, columns=('queue', 'name', 'size', 'status'), show='headings', selectmode='extended')
        for col, title, width in [('queue','Queue',60),('name','File',570),('size','Size',100),('status','Status',160)]:
            self.tree.heading(col, text=title); self.tree.column(col, width=width, stretch=col in ('name','status'))
        scroll = ttk.Scrollbar(area, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side='left', fill='both', expand=True); scroll.pack(side='right', fill='y')
        self.tree.bind('<Double-1>', self.toggle)
        self.tree.bind('<Delete>', self.remove_selected)
        self.tree.bind('<<TreeviewSelect>>', self.update_remove_button)
        self.removebutton = ttk.Button(box, text='Remove selected', command=self.remove_selected, state='disabled')
        self.removebutton.pack(anchor='w', pady=(8, 0))
        self.count = ttk.Label(box); self.count.pack(anchor='w', pady=(8, 3))
        self.bar = ttk.Progressbar(box, maximum=100); self.bar.pack(fill='x', pady=4)
        ttk.Label(box, textvariable=self.status, wraplength=1000).pack(anchor='w', pady=4)
        row = ttk.Frame(box); row.pack(fill='x', pady=(8, 0))
        self.startbutton = ttk.Button(row, text='Start / Resume', command=self.start); self.startbutton.pack(side='left')
        self.pausebutton = ttk.Button(row, text='Pause', command=self.pause, state='disabled'); self.pausebutton.pack(side='left', padx=8)
        self.restorebutton = ttk.Button(row, text='Open saved queue…', command=self.restore); self.restorebutton.pack(side='left')
        ttk.Button(row, text='Open folder', command=self.open_folder).pack(side='right')
        self.controls = [self.urlentry, self.folderentry, self.loadbutton, self.loginbutton, self.browsebutton, self.allbutton, self.nonebutton, self.startbutton, self.restorebutton]
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(100, self.poll)

    def set_busy(self, value):
        self.busy = value
        for widget in self.controls:
            widget.configure(state='disabled' if value else 'normal')
        self.update_remove_button()

    def background(self, fn):
        self.set_busy(True)
        def work():
            try:
                fn()
            except Exception as e:
                self.events.put(('error', str(e)))
            finally:
                self.events.put(('idle', None))
        threading.Thread(target=work, daemon=True).start()

    def load(self):
        try:
            item = identifier(self.url.get())
        except ValueError as e:
            messagebox.showerror('Item URL', str(e)); return
        self.status.set('Loading the complete file list…')
        def work():
            data = self.client.metadata(item)
            entries = [dict(f, queued=True, status='Account required' if str(f.get('private', '')).lower() == 'true' else 'Queued') for f in data['files'] if f.get('name')]
            seen = set()
            for e in entries:
                name = str(local_name(e['name'])).casefold()
                if name in seen or name.split(os.sep)[0] == '.ia-downloader':
                    raise ValueError('This item has conflicting Windows filenames: ' + e['name'])
                seen.add(name)
            self.events.put(('loaded', (item, entries)))
        self.background(work)

    def render(self):
        self.tree.delete(*self.tree.get_children())
        needle = self.filter.get().casefold()
        for i, e in enumerate(self.entries):
            if needle in e['name'].casefold():
                self.tree.insert('', 'end', iid=str(i), values=('Yes' if e['queued'] else '—', e['name'], size_text(int(e.get('size', 0) or 0)), e['status']))
        selected = [e for e in self.entries if e['queued']]
        total = sum(int(e.get('size', 0) or 0) for e in selected)
        self.count.configure(text=f'{len(selected):,} queued / {len(self.entries):,} files • {size_text(total)} listed • Double-click a row to toggle it')

    def update_remove_button(self, event=None):
        self.removebutton.configure(state='normal' if not self.busy and self.tree.selection() else 'disabled')

    def remove_selected(self, event=None):
        if self.busy: return 'break'
        indices = sorted((int(iid) for iid in self.tree.selection()), reverse=True)
        if not indices: return 'break'
        for i in indices:
            del self.entries[i]
        self.render()
        self.update_remove_button()
        self.status.set(f'Removed {len(indices):,} file(s) from the list. Files on disk were kept. Load files again to restore removed items.')
        return 'break'

    def select(self, selected):
        if self.busy: return
        for iid in self.tree.get_children(): self.entries[int(iid)]['queued'] = selected
        self.render()

    def toggle(self, event):
        if self.busy: return
        iid = self.tree.identify_row(event.y)
        if iid:
            self.entries[int(iid)]['queued'] = not self.entries[int(iid)]['queued']
            self.render()

    def browse(self):
        path = filedialog.askdirectory()
        if path: self.folder.set(path)

    def open_folder(self):
        path = Path(self.folder.get())
        if self.item and (path / self.item).is_dir(): path /= self.item
        if path.is_dir(): os.startfile(path)

    def login(self):
        dialog = tk.Toplevel(self.root); dialog.title('Sign in to Archive.org'); dialog.resizable(False, False)
        dialog.transient(self.root); dialog.grab_set()
        frame = ttk.Frame(dialog, padding=20); frame.pack()
        ttk.Label(frame, text='Credentials go directly to Archive.org over HTTPS.\nYour session stays in memory until you close the app.').pack(anchor='w', pady=(0, 12))
        ttk.Label(frame, text='Email').pack(anchor='w')
        email = ttk.Entry(frame, width=48); email.pack(pady=(0, 8)); email.focus_set()
        ttk.Label(frame, text='Password').pack(anchor='w')
        password = ttk.Entry(frame, width=48, show='•'); password.pack(pady=(0, 12))
        def submit():
            user, secret = email.get().strip(), password.get()
            password.delete(0, 'end'); dialog.destroy()
            self.status.set('Signing in…')
            def work():
                self.client.login(user, secret)
                self.events.put(('signedin', None))
            self.background(work)
        ttk.Button(frame, text='Sign in', command=submit).pack(anchor='e')
        dialog.bind('<Return>', lambda _: submit())

    def save(self):
        root = Path(self.folder.get()) / self.item
        path = safe_target(root, '.ia-downloader/queue.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps({'version': 1, 'item': self.item, 'folder': str(Path(self.folder.get()).resolve()), 'files': self.entries}, indent=2), encoding='utf-8')
        temp.replace(path)

    def restore(self):
        path = filedialog.askopenfilename(title='Open queue.json inside the item’s .ia-downloader folder', filetypes=[('Saved queue', '*.json')])
        if not path: return
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8'))
            item = identifier(data['item'])
            entries = data['files']
            seen = set()
            for e in entries:
                key = str(local_name(e['name'])).casefold()
                if key in seen or key.split(os.sep)[0] == '.ia-downloader': raise ValueError('Conflicting filenames.')
                seen.add(key)
                e['queued'] = bool(e.get('queued', True)); e['status'] = 'Queued'
            self.item, self.entries = item, entries
            self.folder.set(data['folder']); self.url.set(BASE + '/details/' + item)
            self.render(); self.status.set('Queue restored. Start / Resume checks existing files before downloading.')
        except Exception as e:
            messagebox.showerror('Cannot open queue', str(e))

    def start(self):
        selected = [(i, dict(e)) for i, e in enumerate(self.entries) if e['queued']]
        if not selected: return
        if not self.folder.get().strip():
            messagebox.showerror('Download folder', 'Choose a folder first.'); return
        try: self.save()
        except Exception as e:
            messagebox.showerror('Cannot save queue', str(e)); return
        folder, item = self.folder.get(), self.item
        self.stop.clear(); self.pausebutton.configure(state='normal')
        def work():
            failed = 0
            for number, (i, entry) in enumerate(selected, 1):
                if self.stop.is_set(): break
                self.events.put(('row', (i, 'Downloading')))
                def progress(done, total):
                    self.events.put(('progress', (number, len(selected), entry['name'], done, total)))
                progress(0, int(entry.get('size', 0) or 0))
                try:
                    result = download(self.client, item, entry, folder, self.stop, progress)
                except Paused:
                    self.events.put(('row', (i, 'Paused'))); break
                except Exception as e:
                    result = 'Failed: ' + str(e); failed += 1
                self.events.put(('row', (i, result)))
            self.events.put(('finished', (self.stop.is_set(), failed)))
        self.background(work)

    def pause(self):
        self.stop.set(); self.status.set('Pausing… the current network read may take up to 30 seconds.')

    def poll(self):
        for _ in range(250):
            try: kind, data = self.events.get_nowait()
            except queue.Empty: break
            if kind == 'loaded':
                self.item, self.entries = data; self.render()
                self.status.set(f'Loaded {len(self.entries):,} files. Choose a folder, then Start / Resume.')
            elif kind == 'signedin':
                self.loginbutton.configure(text='Signed in ✓')
                self.status.set('Signed in for this app session. Load the item or retry your queue.')
            elif kind == 'error':
                self.status.set(data); messagebox.showerror('Archive Downloader', data)
            elif kind == 'idle':
                self.set_busy(False); self.pausebutton.configure(state='disabled')
            elif kind == 'row':
                i, state = data; self.entries[i]['status'] = state
                if self.tree.exists(str(i)): self.tree.set(str(i), 'status', state)
            elif kind == 'progress':
                number, total_files, name, done, total = data
                self.bar['value'] = done * 100 / total if total else 0
                self.status.set(f'File {number:,} / {total_files:,} • {name} • {size_text(done)}' + (f' / {size_text(total)}' if total else ''))
            elif kind == 'finished':
                paused, failed = data
                self.status.set('Paused. Use Start / Resume to continue.' if paused else f'Queue finished. {failed} failed. Start / Resume retries failures and verifies completed files.')
                if not paused: self.bar['value'] = 100
                try: self.save()
                except Exception as e: self.status.set('Could not save queue status: ' + str(e))
        self.root.after(100, self.poll)

    def close(self):
        if self.busy:
            self.pause()
            self.root.after(200, self.close_when_idle)
        else: self.root.destroy()

    def close_when_idle(self):
        if self.busy: self.root.after(200, self.close_when_idle)
        else: self.root.destroy()


if __name__ == '__main__':
    App(tk.Tk()).root.mainloop()
