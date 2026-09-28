# Archive Downloader

Double-click **ArchiveDownloader.exe**. No Python installation is required on Windows x64.

1. Your PSP item URL is already filled in; you can replace it with any Archive.org item URL.
2. For account-restricted files, choose **Sign in…** and enter your email/password in the app. Credentials are sent directly to Archive.org over HTTPS. The password and login session are not saved to disk. Sign in again after reopening the app.
3. Click **Load files**. Every file in the item is queued automatically, including metadata and torrent files.
4. Choose a download folder. The app creates a subfolder named after the item identifier.
5. Click **Start / Resume**.

The supplied PSP item listed 764 files totaling about 440.5 GiB when checked. Most game files are marked as requiring account access. Allow enough disk space for your chosen files.

## Queue controls

- Filter by part of a filename, such as `.zip`, a title, or a region.
- **Queue shown** and **Unqueue shown** apply to all currently visible rows. Filtering alone does not change the queue.
- To select only ZIPs: clear the filter, click Unqueue shown, filter `.zip`, then click Queue shown.
- Double-click a file to toggle it.
- **Pause** keeps partial files. The current network read may need up to 30 seconds to stop.
- **Start / Resume** retries failures and skips existing files only after checking their size and available SHA-1/MD5 checksum.
- To resume after closing, load the same item and choose the same folder, or use **Open saved queue…** and select `<download folder>/<item>/.ia-downloader/queue.json`.
- The queue is saved when downloads start and when the run finishes or pauses. Changes made without starting a run are not saved.

Files download individually, sequentially, with retries and backoff. Nothing is combined or extracted. The app does not download a collection's child items automatically; use a specific item URL.

## Integrity and errors

Partial data stays in the item's `.ia-downloader` directory. Keep that directory to retain resume data. Completed files move to their proper names only after size and available checksum checks. If a server ignores resume requests, the partial file is restarted safely. If no checksum is supplied, verification is limited to available size/HTTP length information.

Existing destination files that do not match are preserved and reported as failures; move or rename them before retrying. Windows-invalid filename characters are escaped with `~` codes. Conflicting Windows filenames are rejected. Extremely long paths may still need a shorter destination, such as `D:\Archive`.

Signing in permits only access granted to your account. HTTP 401/403 files are reported and the queue continues. Successful account login and restricted-file transfers have not been tested with a real account; no credentials were supplied during development.

## Source and tests

`archive_downloader.py` uses only the Python standard library and Tkinter. To run from source with Python 3.11 or later: `python archive_downloader.py`.

Run tests: `python -m unittest -v test_downloader.py`.

Build a Windows executable with PyInstaller: `python -m PyInstaller --onefile --windowed --name ArchiveDownloader archive_downloader.py`.

Protocol references: [Archive.org metadata API](https://archive.org/developers/md-read.html) and [Internet Archive's authentication client](https://github.com/jjjake/internetarchive/blob/master/internetarchive/config.py).

This is an independent utility, not an official Internet Archive application.
