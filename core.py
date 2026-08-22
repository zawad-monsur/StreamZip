"""Streaming ZIP downloader.

Downloads a remote .zip and writes out its *extracted* contents directly,
without ever storing the .zip itself. Peak disk usage is the size of the
extracted data alone, not zip + extracted.

Two modes, picked automatically:

  random-access  the server honours HTTP Range requests. We read the central
                 directory from the tail of the archive, then fetch and inflate
                 each member individually. Gives an accurate file list up front,
                 per-entry resume, and skipping of unwanted members.

  sequential     the server ignores Range. We take one long GET and parse local
                 file headers as the bytes go by. No file list up front, no
                 resume, but still never lands the .zip on disk.

Stdlib only.
"""

from __future__ import annotations

import fnmatch
import http.client
import os
import re
import shutil
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import deque
from dataclasses import dataclass, field

CHUNK = 1 << 20  # 1 MiB network/disk chunk

EOCD_SIG = b"PK\x05\x06"
ZIP64_EOCD_SIG = b"PK\x06\x06"
ZIP64_LOC_SIG = b"PK\x06\x07"
CD_SIG = b"PK\x01\x02"
LFH_SIG = b"PK\x03\x04"
DD_SIG = b"PK\x07\x08"

STORE = 0
DEFLATE = 8
METHOD_NAMES = {0: "store", 8: "deflate", 9: "deflate64", 12: "bzip2",
                14: "lzma", 93: "zstd", 99: "aes"}

USER_AGENT = "streamzip/1.0"


class ZipError(Exception):
    """Something about the archive itself is wrong or unsupported."""


class Cancelled(Exception):
    """The user asked to stop."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def human(n):
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024 or unit == "PB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024.0
    return f"{n} B"


def human_time(seconds):
    if seconds is None or seconds != seconds or seconds < 0 or seconds > 60 * 60 * 24 * 30:
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def dos_to_unix(dostime, dosdate):
    try:
        return time.mktime((
            1980 + ((dosdate >> 9) & 0x7F),
            (dosdate >> 5) & 0x0F,
            dosdate & 0x1F,
            (dostime >> 11) & 0x1F,
            (dostime >> 5) & 0x3F,
            (dostime & 0x1F) * 2,
            0, 0, -1,
        ))
    except (ValueError, OverflowError):
        return None


_WIN_BAD = re.compile('[<>:"|?*\x00-\x1f]')
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _sanitize_component(part):
    if os.name == "nt":
        part = _WIN_BAD.sub("_", part)
        part = part.rstrip(" .") or "_"
        stem = part.split(".")[0].upper()
        if stem in _WIN_RESERVED:
            part = "_" + part
    return part


def safe_join(dest, name):
    """Map a zip member name onto a path inside dest. Blocks zip-slip.

    Returns None for names that resolve to nothing at all.
    """
    name = name.replace("\\", "/")
    parts = []
    for raw in name.split("/"):
        if raw in ("", "."):
            continue
        if raw == "..":
            raise ZipError(f"refusing path escape in archive entry: {name!r}")
        if ":" in raw and not parts:  # drive-letter style prefix
            raw = raw.split(":", 1)[1]
            if not raw:
                continue
        parts.append(_sanitize_component(raw))
    if not parts:
        return None
    path = os.path.join(dest, *parts)
    root = os.path.abspath(dest)
    full = os.path.abspath(path)
    if full != root and not full.startswith(root + os.sep):
        raise ZipError(f"refusing path escape in archive entry: {name!r}")
    return full


def long_path(path):
    """Windows caps paths at 260 chars unless you opt in with this prefix."""
    if os.name != "nt":
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def free_space(path):
    probe = os.path.abspath(path)
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return shutil.disk_usage(probe).free


def filename_from_url(url):
    name = os.path.basename(urllib.parse.urlparse(url).path) or "archive.zip"
    return _sanitize_component(urllib.parse.unquote(name))


# --------------------------------------------------------------------------
# TLS trust
# --------------------------------------------------------------------------

_SSL_CACHE = {}
SERVER_AUTH_OID = "1.3.6.1.5.5.7.3.1"


def ssl_backend():
    """Which trust source we can actually use, best first."""
    try:
        import truststore  # noqa: F401
        return "truststore"
    except ImportError:
        pass
    try:
        import certifi  # noqa: F401
        return "certifi"
    except ImportError:
        pass
    return "windows-store" if os.name == "nt" else "openssl-default"


def make_ssl_context(insecure=False):
    """Build a verifying context that actually works on Windows.

    Python's bundled OpenSSL reads a *cached snapshot* of the Windows root
    store, which is usually a few dozen certs. Windows itself pulls missing
    roots from Windows Update on demand - a path OpenSSL cannot trigger - so
    perfectly valid sites fail with "unable to get local issuer certificate".

    Preference order:
      truststore  hands validation to Windows' own SChannel, roots and all
      certifi     the full Mozilla bundle
      fallback    OpenSSL defaults plus every usable cert in the Windows stores
    """
    key = bool(insecure)
    if key in _SSL_CACHE:
        return _SSL_CACHE[key]

    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        ctx = None
        try:
            import truststore
            ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:
            ctx = None
        if ctx is None:
            ctx = ssl.create_default_context()
            try:
                import certifi
                ctx.load_verify_locations(cafile=certifi.where())
            except Exception:
                pass
            if os.name == "nt":
                for store in ("ROOT", "CA"):
                    try:
                        entries = ssl.enum_certificates(store)
                    except Exception:
                        continue
                    for cert, encoding, trust in entries:
                        if encoding != "x509_asn":
                            continue
                        if trust is not True and not (
                                isinstance(trust, set) and SERVER_AUTH_OID in trust):
                            continue
                        try:
                            ctx.load_verify_locations(cadata=cert)
                        except ssl.SSLError:
                            pass

    _SSL_CACHE[key] = ctx
    return ctx


def is_cert_error(exc):
    text = str(exc)
    return "CERTIFICATE_VERIFY_FAILED" in text or "SSLCertVerificationError" in text         or isinstance(getattr(exc, "reason", None), ssl.SSLCertVerificationError)


def describe_error(exc):
    """Turn a raw exception into something worth showing a human."""
    text = str(getattr(exc, "reason", None) or exc)
    if is_cert_error(exc):
        return ("This server's certificate could not be verified.\n\n"
                "Windows keeps only a partial copy of its root certificates for "
                "Python to read, so this often means a missing root rather than a "
                "real problem with the site.\n\n"
                "Press the Fix certificates button to install the full trust "
                "store (one-off, about 200 KB). If you know the link is safe "
                "and the fix does not help, tick Skip certificate check.\n\n"
                f"Details: {text}")
    if isinstance(exc, urllib.error.HTTPError):
        hints = {401: "The link needs authentication - try the Header field.",
                 403: "Access denied. A signed link may have expired, or a "
                      "Cookie/Authorization header may be required.",
                 404: "Not found - check the link.",
                 429: "The server is rate-limiting. Wait a bit and resume."}
        hint = hints.get(exc.code, "")
        return f"HTTP {exc.code} {exc.reason}. {hint}".strip()
    return text


@dataclass
class Entry:
    name: str
    method: int
    crc: int
    csize: int
    usize: int
    header_offset: int
    flags: int
    mtime: float = None

    @property
    def is_dir(self):
        return self.name.endswith("/")

    @property
    def encrypted(self):
        return bool(self.flags & 0x1)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Remote:
    """A remote file addressed by byte range, with reconnect-on-drop."""

    def __init__(self, url, headers=None, timeout=60.0, retries=8, cancel=None,
                 insecure=False):
        self.url = url
        self.headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
        self.headers.update(headers or {})
        self.timeout = timeout
        self.retries = retries
        self.cancel = cancel or threading.Event()
        self.size = None
        self.supports_ranges = False
        self.final_url = url
        self.insecure = insecure
        self.context = make_ssl_context(insecure)
        self._conn = None
        self._conn_key = None

    def _check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled()

    # -- connection management ---------------------------------------------
    #
    # A fresh TCP+TLS handshake per HTTP request is fine for one big file but
    # ruinous for an archive with thousands of small members - each one costs
    # a full round trip of pure latency before a single byte moves. Keeping
    # one persistent connection per (scheme, host, port) and reusing it across
    # every central-directory read, local-header peek, and data fetch turns
    # that from N handshakes into (usually) one.

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None
        self._conn_key = None

    def _connect(self, scheme, host, port):
        key = (scheme, host, port)
        if self._conn is not None and self._conn_key == key:
            return self._conn, True
        self.close()
        if scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=self.timeout,
                                               context=self.context)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=self.timeout)
        self._conn = conn
        self._conn_key = key
        return conn, False

    def _raise_for_status(self, resp):
        try:
            resp.read()
        except Exception:
            pass
        raise urllib.error.HTTPError(self.final_url, resp.status, resp.reason,
                                     resp.headers, None)

    def _request(self, start=None, end=None, _redirects=0, _retried_stale=False):
        """One GET against the current final_url, over the kept-alive connection.

        Redirects are resolved once and then baked into final_url, so every
        later call goes straight to the real host instead of re-paying the
        redirect hop on every single range request.
        """
        parsed = urllib.parse.urlsplit(self.final_url)
        host = parsed.hostname
        if not host:
            raise ZipError(f"not a valid URL: {self.final_url!r}")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        headers = dict(self.headers)
        if start is not None:
            headers["Range"] = f"bytes={start}-" if end is None else f"bytes={start}-{end}"

        conn, reused = self._connect(parsed.scheme, host, port)
        try:
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()
        except Exception:
            self.close()
            if reused and not _retried_stale:
                # a kept-alive connection can go stale between uses (idle
                # timeout on the server side) - that is routine, not a real
                # failure, so retry once immediately on a fresh connection
                # rather than counting it against the backoff budget.
                return self._request(start, end, _redirects, True)
            raise

        if resp.status in (301, 302, 303, 307, 308) and _redirects < 5:
            location = resp.getheader("Location")
            resp.read()
            if location:
                self.final_url = urllib.parse.urljoin(self.final_url, location)
                self.close()
                return self._request(start, end, _redirects + 1, _retried_stale)

        return resp

    def probe(self):
        """Find the total size and whether byte ranges work."""
        self._check_cancel()
        resp = self._request(0, 0)
        if resp.status in (416, 501):  # range unsupported
            resp.read()
            resp = self._request()
        if resp.status >= 400:
            self._raise_for_status(resp)
        content_range = resp.getheader("Content-Range")
        if resp.status == 206 and content_range:
            self.supports_ranges = True
            total = content_range.rsplit("/", 1)[-1].strip()
            self.size = None if total == "*" else int(total)
        else:
            self.supports_ranges = False
            length = resp.getheader("Content-Length", "")
            self.size = int(length) if length.isdigit() else None
        resp.read()  # drain so the connection is clean for what comes next

    def stream_range(self, start, length, on_net=None):
        """Yield exactly `length` bytes starting at `start`.

        Reconnects transparently on a dropped connection, resuming at the exact
        byte we left off. The caller's decompressor state therefore survives
        network hiccups - important when one member is tens of gigabytes.
        """
        if length == 0:
            return
        pos = 0
        attempt = 0
        while pos < length:
            self._check_cancel()
            try:
                resp = self._request(start + pos, start + length - 1)
                if resp.status >= 400:
                    self._raise_for_status(resp)
                if resp.status != 206:
                    resp.read()
                    raise ZipError("server stopped honouring Range requests mid-download")
                short = False
                while pos < length:
                    self._check_cancel()
                    buf = resp.read(min(CHUNK, length - pos))
                    if not buf:
                        short = True
                        break
                    pos += len(buf)
                    attempt = 0
                    if on_net:
                        on_net(len(buf))
                    yield buf
                if short:
                    # fewer bytes than promised - the connection cannot be
                    # trusted for reuse, and this counts as a real failure
                    self.close()
                    raise IOError(f"connection closed early at byte {start + pos}")
            except GeneratorExit:
                self.close()
                raise
            except (Cancelled, ZipError, urllib.error.HTTPError):
                self.close()
                raise
            except Exception as exc:  # any transport failure retries
                self.close()
                attempt += 1
                if attempt > self.retries:
                    raise ZipError(f"giving up after {self.retries} retries at byte "
                                   f"{start + pos}: {exc}") from exc
                self._sleep(min(2 ** attempt, 30))

    def _sleep(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self._check_cancel()
            time.sleep(0.1)

    def read_range(self, start, length):
        return b"".join(self.stream_range(start, length))

    def open_sequential(self):
        resp = self._request()
        if resp.status >= 400:
            self._raise_for_status(resp)
        return resp


# --------------------------------------------------------------------------
# ZIP structure parsing
# --------------------------------------------------------------------------

def _parse_zip64_extra(extra, usize, csize, offset):
    """Replace 0xFFFF.. placeholders with the real 64-bit values."""
    pos = 0
    while pos + 4 <= len(extra):
        tag, size = struct.unpack_from("<HH", extra, pos)
        pos += 4
        if pos + size > len(extra):
            break
        if tag == 0x0001:
            fp = pos
            end = pos + size
            if usize == 0xFFFFFFFF and fp + 8 <= end:
                usize = struct.unpack_from("<Q", extra, fp)[0]
                fp += 8
            if csize == 0xFFFFFFFF and fp + 8 <= end:
                csize = struct.unpack_from("<Q", extra, fp)[0]
                fp += 8
            if offset == 0xFFFFFFFF and fp + 8 <= end:
                offset = struct.unpack_from("<Q", extra, fp)[0]
                fp += 8
            break
        pos += size
    return usize, csize, offset


def read_central_directory(remote):
    """Fetch and parse the archive's index using range requests."""
    if remote.size is None:
        raise ZipError("server did not report the file size, cannot locate the index")

    tail_len = min(remote.size, 65557 + 64)
    tail = remote.read_range(remote.size - tail_len, tail_len)
    eocd_pos = tail.rfind(EOCD_SIG)
    if eocd_pos < 0:
        raise ZipError("no end-of-central-directory record - is this really a .zip?")

    (_sig, _disk, _cd_disk, _entries_disk, entries,
     cd_size, cd_offset, _comment_len) = struct.unpack_from("<4sHHHHIIH", tail, eocd_pos)

    # Zip64 - anything over 4 GB or 65535 members lives here.
    loc_pos = eocd_pos - 20
    if loc_pos >= 0 and tail[loc_pos:loc_pos + 4] == ZIP64_LOC_SIG:
        _s, _d, z64_offset, _n = struct.unpack_from("<4sIQI", tail, loc_pos)
        z64 = remote.read_range(z64_offset, 56)
        if z64[:4] != ZIP64_EOCD_SIG:
            raise ZipError("zip64 end-of-central-directory record is corrupt")
        (_s, _recsize, _vm, _vn, _d1, _d2,
         _entries_disk, entries, cd_size, cd_offset) = struct.unpack_from("<4sQHHIIQQQQ", z64, 0)

    if cd_size == 0:
        return []
    blob = remote.read_range(cd_offset, cd_size)

    out = []
    pos = 0
    while pos + 46 <= len(blob) and blob[pos:pos + 4] == CD_SIG:
        (_sig, _vmade, _vneed, flags, method, dostime, dosdate, crc,
         csize, usize, nlen, elen, clen, _disk, _iattr, _eattr,
         header_offset) = struct.unpack_from("<4sHHHHHHIIIHHHHHII", blob, pos)
        pos += 46
        raw_name = blob[pos:pos + nlen]
        pos += nlen
        extra = blob[pos:pos + elen]
        pos += elen + clen
        usize, csize, header_offset = _parse_zip64_extra(extra, usize, csize, header_offset)
        encoding = "utf-8" if flags & 0x800 else "cp437"
        try:
            name = raw_name.decode(encoding)
        except UnicodeDecodeError:
            name = raw_name.decode("cp437", "replace")
        out.append(Entry(name=name, method=method, crc=crc, csize=csize, usize=usize,
                         header_offset=header_offset, flags=flags,
                         mtime=dos_to_unix(dostime, dosdate)))
    if entries and entries != 0xFFFF and len(out) != entries:
        raise ZipError(f"index says {entries} members but only {len(out)} parsed")
    return out


def data_offset(remote, entry):
    """Where an entry's compressed bytes actually begin.

    The local header repeats the name and carries its *own* extra field, which
    is often a different length from the central directory's copy.
    """
    head = remote.read_range(entry.header_offset, 30)
    if head[:4] != LFH_SIG:
        raise ZipError(f"bad local header for {entry.name!r}")
    nlen, elen = struct.unpack_from("<HH", head, 26)
    return entry.header_offset + 30 + nlen + elen


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------

@dataclass
class Manifest:
    entries: list
    archive_size: int
    supports_ranges: bool
    url: str

    @property
    def files(self):
        return [e for e in self.entries if not e.is_dir]

    @property
    def total_uncompressed(self):
        return sum(e.usize for e in self.files)

    @property
    def total_compressed(self):
        return sum(e.csize for e in self.files)


@dataclass
class Stats:
    files_done: int = 0
    files_total: int = 0
    files_skipped: int = 0
    bytes_written: int = 0
    bytes_total: int = 0
    bytes_downloaded: int = 0
    started: float = field(default_factory=time.monotonic)
    current: str = ""
    current_written: int = 0
    current_total: int = 0
    samples: deque = field(default_factory=lambda: deque(maxlen=600))

    WINDOW = 8.0   # seconds of history the rolling rates look back over

    def sample(self):
        """Record a point for the rolling rates. Call once per UI refresh."""
        now = time.monotonic()
        self.samples.append((now, self.bytes_downloaded, self.bytes_written))
        cutoff = now - self.WINDOW
        while len(self.samples) > 2 and self.samples[0][0] < cutoff:
            self.samples.popleft()

    def _windowed(self, index):
        """Rate over the recent window, falling back to the run average."""
        if len(self.samples) >= 2:
            first = self.samples[0]
            last = self.samples[-1]
            span = last[0] - first[0]
            if span >= 1.0:
                return max(0.0, (last[index + 1] - first[index + 1]) / span)
        elapsed = time.monotonic() - self.started
        total = self.bytes_downloaded if index == 0 else self.bytes_written
        return total / elapsed if elapsed > 0.5 else 0.0

    @property
    def download_rate(self):
        """Bytes per second coming off the network."""
        return self._windowed(0)

    @property
    def rate(self):
        """Bytes per second landing on disk, after decompression."""
        return self._windowed(1)

    @property
    def eta(self):
        rate = self.rate
        if rate <= 0 or not self.bytes_total:
            return None
        return max(0.0, (self.bytes_total - self.bytes_written) / rate)


class Job:
    """One download-and-extract run.

    All callbacks fire on the worker thread; the GUI marshals them itself.
    """

    def __init__(self, url, dest, headers=None, include=None, exclude=None,
                 skip_existing=True, verify_crc=True, insecure=False,
                 on_log=None, on_progress=None):
        self.url = url
        self.dest = dest
        self.include = include or []
        self.exclude = exclude or []
        self.skip_existing = skip_existing
        self.verify_crc = verify_crc
        self.cancel = threading.Event()
        self.pause = threading.Event()
        self.on_log = on_log or (lambda msg, level="info": None)
        self.on_progress = on_progress or (lambda stats: None)
        self.stats = Stats()
        self.insecure = insecure
        self.remote = Remote(url, headers, cancel=self.cancel, insecure=insecure)

    # -- control -----------------------------------------------------------

    def stop(self):
        self.cancel.set()
        self.pause.clear()

    def _gate(self):
        if self.cancel.is_set():
            raise Cancelled()
        while self.pause.is_set():
            time.sleep(0.15)
            if self.cancel.is_set():
                raise Cancelled()

    def selected(self, entry):
        if entry.is_dir:
            return False
        name = entry.name
        if self.include and not any(fnmatch.fnmatch(name, p) for p in self.include):
            return False
        if any(fnmatch.fnmatch(name, p) for p in self.exclude):
            return False
        return True

    # -- inspection --------------------------------------------------------

    def inspect(self):
        self.remote.probe()
        entries = []
        if self.remote.supports_ranges:
            entries = read_central_directory(self.remote)
        return Manifest(entries, self.remote.size, self.remote.supports_ranges,
                        self.remote.final_url)

    # -- the actual work ---------------------------------------------------

    def run(self, manifest=None):
        os.makedirs(self.dest, exist_ok=True)
        try:
            if manifest is None:
                manifest = self.inspect()
            if manifest.supports_ranges:
                self.on_log(f"Byte ranges supported - {len(manifest.entries)} members, "
                            f"{human(manifest.total_uncompressed)} extracted.")
                self._run_random_access(manifest)
            else:
                self.on_log("Server ignores Range requests - falling back to a single "
                            "sequential pass. No file list up front, and no resume.", "warn")
                self._run_sequential()
        finally:
            self.remote.close()
        return self.stats

    def _run_random_access(self, manifest):
        wanted = [e for e in manifest.entries if self.selected(e)]
        self.stats.files_total = len(wanted)
        self.stats.bytes_total = sum(e.usize for e in wanted)
        self.stats.started = time.monotonic()

        for entry in manifest.entries:
            if entry.is_dir:
                path = safe_join(self.dest, entry.name)
                if path:
                    os.makedirs(long_path(path), exist_ok=True)

        for entry in wanted:
            self._gate()
            if entry.encrypted:
                raise ZipError(f"{entry.name} is password-protected; not supported")
            if entry.method not in (STORE, DEFLATE):
                raise ZipError(f"{entry.name} uses compression method "
                               f"{METHOD_NAMES.get(entry.method, entry.method)}, not supported")
            path = safe_join(self.dest, entry.name)
            if path is None:
                continue
            lp = long_path(path)
            if self.skip_existing and os.path.exists(lp) and os.path.getsize(lp) == entry.usize:
                self.stats.files_skipped += 1
                self.stats.files_done += 1
                self.stats.bytes_written += entry.usize
                self.on_log(f"skip  {entry.name} (already extracted)")
                self.on_progress(self.stats)
                continue
            self._extract_one(entry, path)

    def _extract_one(self, entry, path):
        os.makedirs(long_path(os.path.dirname(path)), exist_ok=True)
        start = data_offset(self.remote, entry)
        part = path + ".streamzip-part"

        self.stats.current = entry.name
        self.stats.current_total = entry.usize
        self.stats.current_written = 0
        self.on_progress(self.stats)

        decomp = zlib.decompressobj(-zlib.MAX_WBITS) if entry.method == DEFLATE else None
        crc = 0
        written = 0
        last_tick = 0.0

        def on_net(n):
            self.stats.bytes_downloaded += n

        try:
            with open(long_path(part), "wb") as out:
                for chunk in self.remote.stream_range(start, entry.csize, on_net):
                    self._gate()
                    data = decomp.decompress(chunk) if decomp else chunk
                    if data:
                        out.write(data)
                        crc = zlib.crc32(data, crc)
                        written += len(data)
                        self.stats.bytes_written += len(data)
                        self.stats.current_written = written
                    now = time.monotonic()
                    if now - last_tick > 0.1:
                        last_tick = now
                        self.on_progress(self.stats)
                if decomp:
                    tail = decomp.flush()
                    if tail:
                        out.write(tail)
                        crc = zlib.crc32(tail, crc)
                        written += len(tail)
                        self.stats.bytes_written += len(tail)
                        self.stats.current_written = written
        except BaseException:
            try:
                os.remove(long_path(part))
            except OSError:
                pass
            raise

        self._finish(entry.name, part, path, entry.usize, written,
                     entry.crc, crc, entry.mtime)

    def _finish(self, name, part, path, usize, written, crc_expected, crc_actual, mtime):
        if usize and written != usize:
            os.remove(long_path(part))
            raise ZipError(f"{name}: expected {usize} bytes, wrote {written}")
        if self.verify_crc and crc_expected and (crc_actual & 0xFFFFFFFF) != crc_expected:
            os.remove(long_path(part))
            raise ZipError(f"{name}: CRC mismatch - the data arrived corrupt")
        os.replace(long_path(part), long_path(path))
        if mtime:
            try:
                os.utime(long_path(path), (mtime, mtime))
            except OSError:
                pass
        self.stats.files_done += 1
        self.stats.current_written = 0
        self.on_log(f"ok    {name}  ({human(written)})")
        self.on_progress(self.stats)

    # -- sequential fallback ----------------------------------------------

    def _run_sequential(self):
        self.stats.started = time.monotonic()
        with self.remote.open_sequential() as resp:
            def on_net(n):
                self.stats.bytes_downloaded += n
            reader = _SeqReader(resp, self._gate, on_net)
            while True:
                self._gate()
                sig = reader.read_exact(4, allow_eof=True)
                if len(sig) < 4 or sig in (CD_SIG, EOCD_SIG):
                    break
                if sig != LFH_SIG:
                    raise ZipError(f"unexpected signature {sig!r} in stream")
                self._sequential_entry(reader)

    def _sequential_entry(self, reader):
        head = reader.read_exact(26)
        (_ver, flags, method, dostime, dosdate, crc_expected,
         csize, usize, nlen, elen) = struct.unpack("<HHHHHIIIHH", head)
        raw_name = reader.read_exact(nlen)
        extra = reader.read_exact(elen)
        usize, csize, _ = _parse_zip64_extra(extra, usize, csize, 0)
        name = raw_name.decode("utf-8" if flags & 0x800 else "cp437", "replace")
        zip64 = len(extra) >= 4 and struct.unpack_from("<H", extra, 0)[0] == 0x0001
        mtime = dos_to_unix(dostime, dosdate)

        if flags & 0x1:
            raise ZipError(f"{name} is password-protected; not supported")
        if method not in (STORE, DEFLATE):
            raise ZipError(f"{name} uses compression method "
                           f"{METHOD_NAMES.get(method, method)}, not supported")

        streamed = bool(flags & 0x8) and csize == 0
        if streamed and method == STORE:
            raise ZipError(f"{name} is stored with a trailing size record; this archive "
                           "can only be extracted from a server that supports byte ranges")

        path = safe_join(self.dest, name)
        if name.endswith("/") or path is None:
            if path:
                os.makedirs(long_path(path), exist_ok=True)
            if csize:
                reader.discard(csize)
            return

        os.makedirs(long_path(os.path.dirname(path)), exist_ok=True)
        part = path + ".streamzip-part"
        decomp = zlib.decompressobj(-zlib.MAX_WBITS) if method == DEFLATE else None
        crc = 0
        written = 0
        remaining = csize
        self.stats.current = name
        self.stats.current_total = usize
        self.stats.current_written = 0

        try:
            with open(long_path(part), "wb") as out:
                while True:
                    self._gate()
                    if streamed:
                        chunk = reader.read_some(CHUNK)
                    else:
                        if remaining <= 0:
                            break
                        chunk = reader.read_some(min(CHUNK, remaining))
                        remaining -= len(chunk)
                    if not chunk:
                        raise ZipError(f"stream ended inside {name}")
                    data = decomp.decompress(chunk) if decomp else chunk
                    if data:
                        out.write(data)
                        crc = zlib.crc32(data, crc)
                        written += len(data)
                        self.stats.bytes_written += len(data)
                        self.stats.current_written = written
                        self.on_progress(self.stats)
                    if decomp and decomp.eof:
                        reader.push(decomp.unused_data)
                        break
                if decomp and not decomp.eof:
                    tail = decomp.flush()
                    if tail:
                        out.write(tail)
                        crc = zlib.crc32(tail, crc)
                        written += len(tail)
                        self.stats.bytes_written += len(tail)
                        self.stats.current_written = written
        except BaseException:
            try:
                os.remove(long_path(part))
            except OSError:
                pass
            raise

        if flags & 0x8:
            crc_expected, usize = reader.read_data_descriptor(crc & 0xFFFFFFFF, written, zip64)

        self.stats.files_total = self.stats.files_done + 1
        self._finish(name, part, path, usize, written, crc_expected, crc, mtime)


class _SeqReader:
    """Buffered forward-only reader over an HTTP response, with pushback."""

    def __init__(self, fp, gate, on_net):
        self.fp = fp
        self.gate = gate
        self.on_net = on_net
        self.buf = b""

    def push(self, data):
        if data:
            self.buf = data + self.buf

    def _fill(self, n):
        self.gate()
        data = self.fp.read(n)
        if data:
            self.on_net(len(data))
        return data

    def read_some(self, n):
        if self.buf:
            out, self.buf = self.buf[:n], self.buf[n:]
            return out
        return self._fill(n)

    def read_exact(self, n, allow_eof=False):
        out = bytearray()
        while len(out) < n:
            chunk = self.read_some(n - len(out))
            if not chunk:
                if allow_eof:
                    return bytes(out)
                raise ZipError("archive ended unexpectedly")
            out += chunk
        return bytes(out)

    def discard(self, n):
        while n > 0:
            chunk = self.read_some(min(CHUNK, n))
            if not chunk:
                raise ZipError("archive ended unexpectedly")
            n -= len(chunk)

    def read_data_descriptor(self, crc_actual, size_actual, zip64):
        """Sizes trail the data here. The layout is ambiguous, so try each
        variant and keep whichever agrees with what we just computed."""
        blob = self.read_exact(24, allow_eof=True)
        for has_sig in (True, False):
            for wide in ((True, False) if zip64 else (False, True)):
                off = 4 if has_sig else 0
                fmt = "<IQQ" if wide else "<III"
                width = off + struct.calcsize(fmt)
                if width > len(blob):
                    continue
                if has_sig and blob[:4] != DD_SIG:
                    continue
                crc, _csize, usize = struct.unpack_from(fmt, blob, off)
                if crc == crc_actual and usize == size_actual:
                    self.push(blob[width:])
                    return crc, usize
        # Nothing matched - trust our own tally and put the bytes back.
        self.push(blob)
        return crc_actual, size_actual
