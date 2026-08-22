# StreamZip — Project Log

Written for whoever (human or AI) picks this project up next, in a session
with no memory of how it got here. Read this before touching the code.

## TL;DR

- **Status: shipped.** Public repo, MIT licensed, `v1.0.0` released with a
  working standalone Windows `.exe` attached.
- Core engine (`core.py`) and GUI (`app.py`/`theme.py`) are both complete and
  were exercised against real test scenarios during development — but **no
  automated tests are committed to this repo**. See "Testing" below; that's
  the biggest real gap.
- One open question asked of the project owner and not yet answered: whether
  to add a custom `.exe` icon (currently uses PyInstaller's default).

## What this is

Desktop app (Windows, built and tested there; architecturally portable —
see "Platform notes" in README): paste a `.zip` URL, it downloads and
extracts in a single streaming pass. **The `.zip` itself is never written to
disk** — only the extracted files land on disk.

The motivating scenario: a 95 GB zip on a drive with 100 GB free. The normal
download-then-unzip flow needs ~195 GB (archive + contents) and simply can't
run. Streaming inflate as bytes arrive means only the ~100 GB of extracted
content ever needs to exist on disk.

## Architecture

| File | Role |
|---|---|
| `core.py` | The engine. HTTP, zip/Zip64 parsing, streaming inflate, extraction orchestration (`Job` class), TLS trust handling. **Zero third-party dependencies, stdlib only, no GUI imports** — this is deliberate, keep it that way. Usable standalone: `core.Job(url, dest).run()`. |
| `app.py` | Tkinter GUI. Runs `core.Job` on a worker thread; callbacks (`on_log`, `on_progress`) push onto a `queue.Queue` that the Tk main loop drains via `.after()` polling — never touch Tk widgets from the worker thread directly. |
| `theme.py` | The dark-mode visual layer: plain `tk` widgets + `Canvas` drawing, not `ttk`. (ttk on Windows refuses to fully recolor native widgets, which is why the very first version looked like Windows 95.) Building blocks: `Card`, `Button`, `Entry`, `ProgressBar`, `Pill` (status chip), `Stat` (metric tile), `Collapsible` (foldable section). |
| `StreamZip.bat` | Windows launcher — prefers `pythonw` (no console window), falls back to `python.exe` if `pythonw` is missing. |
| `docs/screenshot-*.png` | README screenshots. **The "progress" screenshot uses synthetic `Stats` values rendered through the real UI — it is not a real transfer.** Don't mistake those numbers for a benchmark claim if you see them referenced anywhere. |
| `README.md`, `LICENSE` (MIT), `.gitignore` | Standard OSS scaffolding. |

`build/`, `dist/`, `StreamZip.spec`, `__pycache__/` are PyInstaller/Python
artifacts, all gitignored — not committed, regenerable with the command in
the README's "Packaging to a single .exe" section.

## Design decisions, and why (chronological)

### 1. Two extraction modes, auto-selected

- **Random-access** (server honors HTTP `Range`): read the end-of-central-
  directory + central directory via range requests first (a few KB), get the
  full file list and sizes up front. Enables a pre-flight disk-space check,
  resume (skip already-extracted files at the right size), and selective
  extraction via include/exclude glob patterns.
- **Sequential** (server ignores `Range`): one long GET, parse local file
  headers as bytes arrive, inflate on the fly. No file list up front, no
  resume, no selective extraction — but the zip still never touches disk.
- Decided in `Remote.probe()`: a 1-byte range request either comes back
  `206 Partial Content` with a `Content-Range` header, or it doesn't.

### 2. Never write the `.zip` — stream straight to extracted files

Each member is decompressed via `zlib.decompressobj` as HTTP chunks arrive
(1 MiB read chunks). Output files are written as `<name>.streamzip-part`,
verified (size + CRC-32), then `os.replace()`'d to the final name — a
half-written file never looks finished, and a crash or cancel mid-file
leaves no corrupt "final" file behind.

### 3. Zip64 throughout

Needed because the whole point of this tool is archives that blow past the
classic zip format's 4 GB / 65,535-entry limits. `_parse_zip64_extra()`
handles the `0xFFFFFFFF`-placeholder extra-field pattern in both the central
directory and, separately, local headers/data descriptors in sequential mode
(the two can legitimately disagree in field length, which is why
`data_offset()` peeks the local header rather than trusting the central
directory's offsets blindly).

### 4. Security: zip-slip protection, Windows filename sanitization

`safe_join()` rejects `../` path traversal in archive entry names before any
file is created. `_sanitize_component()` remaps Windows-illegal characters
and reserved device names (`CON`, `NUL`, `COM1`, ...) so a malicious or just
oddly-named archive entry can't write outside the destination or collide
with a device name. `long_path()` adds the `\\?\` prefix so Windows' 260-char
path limit doesn't bite on deeply nested archives.

### 5. Reliability: reconnect-and-resume on dropped connections

`stream_range()` retries with exponential backoff (up to 8 attempts) on any
transport failure, resuming from the exact byte offset — the in-memory
decompressor state survives the reconnect, so a network blip during a 40 GB
member costs a retry, not a restart. Verified with a test harness that
deliberately truncates responses mid-stream (see "Testing").

### 6. Certificate handling — real Windows bug, found and fixed mid-session

Hit `CERTIFICATE_VERIFY_FAILED` on a real link during testing. Root cause:
Windows caches only a partial root-certificate store on disk (a few dozen
certs) and normally fetches missing roots from Windows Update on demand — a
path Python's bundled OpenSSL cannot trigger, so otherwise-valid sites fail
to verify.

Fix, in `make_ssl_context()`: prefer the `truststore` package (defers TLS
verification to Windows' own SChannel — the actually-correct fix), fall back
to `certifi`'s bundle, fall back further to manually enumerating
`ssl.enum_certificates()` from the Windows cert store. The GUI has a **Fix
certificates** button that runs `pip install truststore certifi` on demand,
and a **Skip certificate check** escape hatch for a link the user trusts and
can't fix another way.

This panel was briefly hidden behind a collapsed "Advanced options" section
during the UI redesign and the user couldn't find it — fixed by defaulting
that section to open.

### 7. Performance bug — found and fixed from a user report of "very slow"

Original implementation called `urllib.request.urlopen()` fresh every time —
every file needed 2+ brand-new TCP+TLS handshakes (one to peek the local
header via `data_offset()`, one for the actual data), and any redirect was
re-followed on every single request. For an archive with many small members
this is almost pure idle handshake latency contaminating the reported speed.

Rewrote the HTTP layer on `http.client` with one persistent, reused
connection per `(scheme, host, port)`, plus one-time redirect resolution
cached into `Remote.final_url`. Verified with a connection-counting test
harness: a 60-file archive went from ~123 TCP connections to **1**.

Also handles, deliberately:
- Stale keep-alive connections get one free immediate retry (doesn't burn
  the backoff budget — this is routine, not a real failure).
- Servers that refuse keep-alive entirely (`Connection: close`) still work,
  just falls back to reconnect-per-request.
- `GeneratorExit` is caught explicitly in `stream_range()` so a
  cancelled/interrupted stream closes its connection rather than leaving a
  half-read socket sitting in the reuse pool to corrupt the next request.

### 8. Speed display was misleading — split into two numbers

Originally showed one `/s` figure that was actually the post-decompress disk
write rate, which reads higher than actual network throughput for a
compressed archive — confusing next to a user's known internet speed. Added
`Stats.download_rate` (network bytes/sec, separate rolling 8-second-window
average via `Stats.sample()`, called once per UI tick) alongside the
existing write rate. Verified against a throttled test server: reported
download rate landed within ~2-4% of the real throttle rate.

### 9. UI redesign

Original UI was stock `ttk`, which looks like Windows 95 on Windows because
`ttk` won't fully recolor most native widgets there. Rebuilt in `theme.py` as
a dark-mode UI using plain `tk` + `Canvas` (rounded progress bars, a status
pill, big stat tiles for speed/extracted/files/ETA, collapsible Advanced
Options and Activity Log sections). Advanced Options defaults **open**, not
collapsed — user feedback was that Fix Certificates and the Header field
were hard to find when hidden.

## Testing performed (not committed — see gap below)

All testing during development was done with ad-hoc Python scripts in the
session's scratchpad temp directory, which no longer exists and was never
part of this repo. They spun up local `http.server` instances to simulate
real hosts. Scenarios covered:

- Basic extraction correctness in both modes, resume, include/exclude
  filters, corrupted-payload rejection (CRC mismatch), no orphaned
  `.streamzip-part` files left behind after a failure.
- Zip64 (forced 64-bit fields + trailing data descriptors) in both modes.
- 4 deliberately dropped mid-transfer connections — reconnect-and-resume
  produced byte-identical output to a clean run.
- Download-speed accuracy against a throttled test server (reported vs.
  actual, within a few percent).
- TCP connection counting — the keep-alive fix's actual proof: 60-file
  archive, 1 connection versus ~123 before the fix.
- A redirecting URL — confirmed the redirect is resolved once, not
  re-followed on every range request.
- A server that sends `Connection: close` on every response — confirmed
  still-correct output despite zero keep-alive.

**Gap: none of this is in the repo.** If you're picking this up fresh, there
is currently no automated test suite here — only the manual verification
above happened, and it's gone. All of those scripts were small,
self-contained, `http.server`-based, and would translate directly into a
`tests/` directory using `unittest` or `pytest`. That's the single most
valuable thing to add next if you want confidence before changing the HTTP
or zip-parsing logic.

## Repo & release state (as of this log)

- Public repo: <https://github.com/zawad-monsur/StreamZip> (MIT licensed).
- `main` branch, 2 commits: initial commit, then a commit adding the README
  screenshots and download link. Working tree clean.
- Tag `v1.0.0` pushed; GitHub release published at
  <https://github.com/zawad-monsur/StreamZip/releases/tag/v1.0.0> with
  `StreamZip.exe` attached (PyInstaller `--onefile --windowed` build,
  verified to launch standalone with no Python installed on the build
  machine).
- No custom `.exe` icon yet — uses PyInstaller's default. Was asked about,
  unanswered as of this log.
- `gh` CLI is authenticated on the machine this was built on, as
  `zawad-monsur` (device-flow login; token lives in Windows Credential
  Manager there). Future `gh release`/`gh pr`/etc. from that machine should
  work without re-authenticating. This does **not** transfer to a different
  machine or session.

## Known limitations (by design, not bugs)

- Password-protected zip entries: not supported. Reported by name before
  anything is written, not a silent failure.
- Compression methods other than store/deflate (bzip2, LZMA, zstd,
  deflate64, WinZip AES): not supported, same clear-failure behavior.
- Sequential mode (servers without `Range` support) has no file list, no
  resume, no selective extraction — those three are random-access-only.

## Open items / natural next steps

1. Custom `.exe` icon — asked, no answer yet.
2. No automated tests in-repo (the real gap — see "Testing").
3. Cross-platform behavior (Linux/macOS) is architecturally supported — every
   Windows-specific code path is behind an `os.name == "nt"` guard — but was
   never actually run or verified outside Windows.
4. No CI (GitHub Actions). Builds and releases so far are manual, from one
   developer machine.
5. If the project grows contributors: consider whether `core.py`'s
   zero-dependency constraint should become an explicit, enforced rule
   (e.g., a CI check) rather than just a convention someone has to remember.
