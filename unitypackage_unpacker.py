"""unitypackage unpacker: unpack a .unitypackage into the same folders and file names Unity would create.

  unitypackage-unpacker <file.unitypackage> ... [--out DIR] [--no-meta] [--gui]
  unitypackage-unpacker --install      add "unpack .unitypackage" to the Explorer right-click menu (current user)
  unitypackage-unpacker --uninstall    remove it again

A .unitypackage is a gzip-compressed tar. Each asset sits in a folder named after its GUID that holds
asset (the file), asset.meta and pathname (its original path, e.g. Assets/Foo/Bar.png).
This tool reads pathname and writes every asset back to its original place.

- Without --out, it creates a folder named after the package next to it (adds " (2)" if taken).
- Entries whose pathname contains ".." or an absolute path are skipped instead of written outside,
  and so are entries whose path differs only in letter case from one already taken.
- gzip is decompressed in parallel with rapidgzip when available, otherwise with the standard library.
- The tar stream is parsed directly, and disk writes run on a separate thread. A file cut short by
  an error or a cancel is deleted, so no partial file is left behind.
- Without a console (pythonw or the windowed exe) or with --gui, a progress window is shown.
  It closes quietly on success and only reports failures and skipped entries.
"""
import argparse, gzip, itertools, json, os, queue, re, shutil, sys, threading, time

BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
RESERVED_NAMES = ({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
                  | {p + d for p in ("COM", "LPT") for d in "123456789¹²³"})
GZIP_MAGIC = b"\x1f\x8b"
ZERO_BLOCK = b"\0" * 512
CHUNK = 4 * 1024 * 1024
MAX_META = 1024 * 1024            # pax headers, long names and pathname files are a few bytes in real packages
HOLD_LIMIT = 64 * 1024 * 1024     # small entries waiting for their pathname are kept in memory up to this total
TITLE = "unitypackage unpacker"
DECODER = ""
MENU_LABEL = "unpack .unitypackage"
__version__ = "1.0.0"


class Cancelled(Exception):
    pass


def _long(path):
    """Absolute path with the extended-length prefix, so Windows does not apply the 260-character limit."""
    path = os.path.abspath(path)
    if os.name != "nt" or path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def _plain(text):
    """Drop the extended-length prefix from paths in a message."""
    return str(text).replace("\\\\?\\UNC\\", "\\\\").replace("\\\\?\\", "")


def safe_relpath(raw):
    """Turn the first line of pathname into a relative path inside the output folder, or None."""
    p = raw.splitlines()[0].strip() if raw else ""
    p = p.replace("\\", "/")
    if not p or p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        return None
    parts = []
    for part in p.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        part = BAD_CHARS.sub("_", part).rstrip(" .")
        if not part:
            return None
        if part.split(".", 1)[0].rstrip(" ").upper() in RESERVED_NAMES:   # device names such as CON or COM1
            part = "_" + part
        parts.append(part)
    return os.path.join(*parts) if parts else None


def unique_dir(base):
    """Create base, or base (2), base (3) ... if taken, and return the one created."""
    for n in itertools.count(1):
        d = base if n == 1 else "%s (%d)" % (base, n)
        try:
            os.mkdir(_long(d))
            return d
        except FileExistsError:
            pass


def _is_gzip(path):
    with open(path, "rb") as f:
        return f.read(2) == GZIP_MAGIC


def open_stream(path, gz):
    """Return (tar stream, close function)."""
    global DECODER
    if not gz:   # a plain tar, which some packers produce
        DECODER = "none"
        f = open(path, "rb", buffering=CHUNK)
        return f, f.close
    try:
        import rapidgzip
    except ImportError as e:
        DECODER = "gzip (%s)" % e
        g = gzip.open(path, "rb")
        return g, g.close
    # Passed as a descriptor: rapidgzip cannot open a path with non-ASCII characters on Windows,
    # and reading through a Python file object is many times slower.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        # more than 8 threads mostly adds buffer memory, since the disk writes set the pace
        f = rapidgzip.open(fd, parallelization=min(8, os.cpu_count() or 4))
    except BaseException:
        os.close(fd)
        raise
    DECODER = "rapidgzip"

    def close():
        f.close()
        try:
            os.close(fd)   # rapidgzip leaves the descriptor open
        except OSError:
            pass

    return f, close


def _read_upto(stream, n):
    b = stream.read(n)
    while 0 < len(b) < n:
        more = stream.read(n - len(b))
        if not more:
            break
        b += more
    return b


def _read_exact(stream, n):
    b = _read_upto(stream, n)
    if len(b) < n:
        raise EOFError("the package is incomplete")
    return b


def _broken():
    return ValueError("not a unitypackage, or the file is damaged")


def _tar_entries(stream, need_marker):
    """Yield (name, size, is_regular_file) for each tar entry.
    The caller must read exactly size bytes before asking for the next entry. Handles pax (x) and GNU long names (L).
    With need_marker, an archive that stops before the end-of-archive block is treated as incomplete."""
    long_name = None
    pax = {}
    while True:
        hdr = _read_upto(stream, 512)
        if hdr == ZERO_BLOCK or (not hdr and not need_marker):
            return
        if len(hdr) < 512:
            raise EOFError("the package is incomplete")
        try:
            chk = int(hdr[148:156].split(b"\0", 1)[0].strip() or b"0", 8)
        except ValueError:
            raise _broken()
        unsigned = sum(hdr[:148]) + 8 * 32 + sum(hdr[156:])
        if chk != unsigned and chk != unsigned - 256 * sum(c >= 128 for c in hdr[:148] + hdr[156:]):
            raise _broken()
        name = hdr[0:100].split(b"\0", 1)[0]
        if hdr[257:262] == b"ustar":
            prefix = hdr[345:500].split(b"\0", 1)[0]
            if prefix:
                name = prefix + b"/" + name
        raw_size = hdr[124:136]
        if raw_size[0] & 0x80:   # base-256 size (over 8 GB)
            size = int.from_bytes(raw_size[1:], "big")
        else:
            try:
                size = int(raw_size.split(b"\0", 1)[0].strip() or b"0", 8)
            except ValueError:
                raise _broken()
        kind = hdr[156:157]
        if size < 0:
            raise _broken()
        pad = (-size) % 512
        if kind in (b"x", b"g", b"L"):
            if size > MAX_META:
                raise _broken()
            data = _read_exact(stream, size)
            _read_exact(stream, pad)
            if kind == b"L":
                long_name = data.split(b"\0", 1)[0]
            elif kind == b"x":
                for rec in data.decode("utf-8", "replace").split("\n"):   # "<length> <key>=<value>"
                    k, sep, v = rec.partition(" ")[2].partition("=")
                    if sep:
                        pax[k] = v
            continue
        text = pax.get("path") or (long_name or name).decode("utf-8", "replace")
        if "size" in pax:
            try:
                size = int(pax["size"])
            except ValueError:
                raise _broken()
            if size < 0:
                raise _broken()
            pad = (-size) % 512
        long_name, pax = None, {}
        yield text, size, kind in (b"0", b"\0", b"7")
        if pad:
            _read_exact(stream, pad)


class _Writer(threading.Thread):
    """Writes files on a separate thread so disk I/O overlaps with decompression.
    A file cut short by an error, or still open when the work is abandoned, is deleted."""

    def __init__(self, prog):
        super().__init__(daemon=True)
        self.q = queue.Queue(maxsize=16)
        self.error = None
        self.prog = prog

    def run(self):
        f = path = None
        prog = self.prog
        while True:
            op = self.q.get()
            if op is None:
                break
            if self.error:
                continue
            try:
                cmd = op[0]
                if cmd == "open":
                    path = op[1]
                    f = open(path, "wb", buffering=0)
                elif cmd == "write":
                    f.write(op[1])
                    prog.advance(op[2])
                elif cmd == "close":
                    f.close()
                    f = path = None
                    self._done(op[1])
                elif cmd == "file":            # a whole small file: path, data, position, label
                    path = op[1]
                    with open(path, "wb", buffering=0) as f:
                        f.write(op[2])
                    f = path = None
                    prog.advance(op[3])
                    self._done(op[4])
                elif cmd == "replace":
                    os.replace(op[1], op[2])
                    self._done(op[3])
                elif cmd == "makedirs":
                    os.makedirs(op[1], exist_ok=True)
                elif cmd == "mark":
                    prog.advance(op[1])
            except BaseException as e:
                self.error = e
                f, path = self._discard(f, path)
        self._discard(f, path)

    def _done(self, label):
        if label:   # an asset written to its final path
            self.prog.files += 1
            self.prog.current = label

    @staticmethod
    def _discard(f, path):
        if f is not None:
            try:
                f.close()
            except OSError:
                pass
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
        return None, None

    def put(self, *op):
        if self.error:
            raise self.error
        self.q.put(op)

    def finish(self):
        self.q.put(None)
        self.join()


def uncompressed_size(path):
    """Estimate the unpacked size from the gzip ISIZE trailer, which holds the size modulo 2^32."""
    size = os.path.getsize(path)
    if not _is_gzip(path):
        return size
    with open(path, "rb") as f:
        f.seek(max(size - 4, 0))
        isize = int.from_bytes(f.read(4), "little")
    while isize < size * 0.9:      # wrapped past 4 GiB
        isize += 1 << 32
    # packages are mostly compressed textures and models and unpack to about 1-2 times their size,
    # so a wrap that implies more than 20 times means the trailer belongs to the last of several gzip members
    return isize if isize <= size * 20 else size


class Progress:
    """done and total are positions in the unpacked tar; done only advances once data is written."""

    def __init__(self, total):
        self.total = total
        self.done = 0
        self.files = 0
        self.current = ""
        self.cancel = False
        self.started = time.time()

    def advance(self, pos):
        self.done = max(self.done, pos)
        while self.done > self.total:   # the estimate was short by a 4 GiB wrap
            self.total += 1 << 32


def extract(pkg, out=None, with_meta=True, progress=None):
    gz = _is_gzip(pkg)
    prog = progress or Progress(uncompressed_size(pkg))
    prog.started = time.time()
    stream, close_stream = open_stream(pkg, gz)
    writer = _Writer(prog)
    created = False
    work = None
    paths, pending = {}, {}   # guid -> relative path / guid -> {kind: bytes held in memory or a temp file}
    bad, unsafe, dupes = set(), set(), set()   # guids not written / with unsafe paths / with a path already taken
    taken = {}                # normcased relative path -> guid
    made_dirs = set()
    has_asset = set()         # guids that have an asset; the rest are folders
    tmp_names = itertools.count()
    held = 0                  # bytes of entries kept in memory until their pathname arrives
    folders = 0

    def ensure_dir(d):
        if d not in made_dirs:
            writer.put("makedirs", d)
            made_dirs.add(d)

    def check_cancel():
        if prog.cancel:
            raise Cancelled()

    def stream_to(dst, size):
        writer.put("open", dst)
        left = size
        while left > 0:
            check_cancel()
            b = stream.read(min(CHUNK, left))
            if not b:
                raise EOFError("the package is incomplete")
            left -= len(b)
            writer.put("write", b, stream.tell())
        writer.put("close", "")

    def skip(size):
        left = size
        while left > 0:
            check_cancel()
            b = stream.read(min(CHUNK, left))
            if not b:
                raise EOFError("the package is incomplete")
            left -= len(b)

    def place(guid, kind, size):
        """Write an entry whose final path is known: small ones in one go, large ones through a temp file."""
        dst = os.path.join(root, paths[guid] + (".meta" if kind == "asset.meta" else ""))
        ensure_dir(os.path.dirname(dst))
        label = paths[guid] if kind == "asset" else ""
        if size <= CHUNK:
            writer.put("file", dst, _read_exact(stream, size), stream.tell(), label)
            return
        if label:
            prog.current = label
        tmp = os.path.join(work, str(next(tmp_names)))
        stream_to(tmp, size)
        writer.put("replace", tmp, dst, label)

    try:
        if out:
            out = os.path.abspath(out)
            created = not os.path.exists(_long(out))
            os.makedirs(_long(out), exist_ok=True)
        else:
            out = unique_dir(os.path.splitext(os.path.abspath(pkg))[0])
            created = True
        root = _long(out)
        work = os.path.join(root, ".unpack_tmp-%d" % os.getpid())
        os.makedirs(work, exist_ok=True)
        writer.start()
        for name, size, is_file in _tar_entries(stream, gz):
            check_cancel()
            name = name.replace("\\", "/").lstrip("./")
            if not is_file or "/" not in name:
                skip(size)
                continue
            guid, kind = name.split("/", 1)
            if kind == "asset":
                has_asset.add(guid)
            if kind == "pathname":
                if guid in paths or guid in bad:   # a repeated pathname: the first one decides
                    skip(size)
                    continue
                rel = None
                if size > MAX_META:
                    skip(size)
                else:
                    rel = safe_relpath(_read_exact(stream, size).decode("utf-8", "replace"))
                if rel and rel.split(os.sep, 1)[0].lower().startswith(".unpack_tmp"):
                    rel = None
                if rel is None:
                    unsafe.add(guid)
                elif taken.setdefault(os.path.normcase(rel), guid) != guid:
                    dupes.add(guid)
                else:
                    paths[guid] = rel
                    for k, v in pending.pop(guid, {}).items():
                        dst = os.path.join(root, rel + (".meta" if k == "asset.meta" else ""))
                        ensure_dir(os.path.dirname(dst))
                        label = rel if k == "asset" else ""
                        if isinstance(v, bytes):
                            held -= len(v)
                            writer.put("file", dst, v, stream.tell(), label)
                        else:
                            writer.put("replace", v, dst, label)
                    writer.put("mark", stream.tell())
                    continue
                bad.add(guid)
                for v in pending.pop(guid, {}).values():   # temp files go with the temp folder
                    if isinstance(v, bytes):
                        held -= len(v)
            elif kind in ("asset", "asset.meta") and guid not in bad and (with_meta or kind == "asset"):
                if guid in paths:
                    place(guid, kind, size)
                elif size <= CHUNK and held + size <= HOLD_LIMIT:
                    pending.setdefault(guid, {})[kind] = _read_exact(stream, size)
                    held += size
                else:
                    tmp = os.path.join(work, str(next(tmp_names)))
                    stream_to(tmp, size)
                    pending.setdefault(guid, {})[kind] = tmp
            else:
                skip(size)
        for guid, rel in paths.items():   # a guid without an asset is a folder
            if guid not in has_asset:
                ensure_dir(os.path.join(root, rel))
                folders += 1
        if gz:
            try:
                while stream.read(CHUNK):   # read on to the gzip trailer so its checksum and length are verified
                    check_cancel()
            except gzip.BadGzipFile as e:
                if not str(e).startswith("Not a gzipped file"):   # bytes after the last member are ignored
                    raise
        writer.put("mark", stream.tell())
        writer.finish()
        if writer.error:
            raise writer.error
        if not paths and not bad:
            raise ValueError("there is nothing to unpack in this file")
    except BaseException:
        if writer.is_alive():
            writer.finish()
        close_stream()
        if created:
            shutil.rmtree(_long(out), ignore_errors=True)
        elif work:
            shutil.rmtree(work, ignore_errors=True)
        raise
    close_stream()
    shutil.rmtree(work, ignore_errors=True)
    prog.done = prog.total
    return {"out": out, "files": prog.files, "folders": folders,
            "skipped_unsafe": len(unsafe), "duplicate_paths": len(dupes & has_asset), "no_pathname": len(pending),
            "seconds": round(time.time() - prog.started, 2), "decoder": DECODER}


def fmt_time(s):
    s = int(s)
    return "%02d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60)


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def _msg(text, error=False):
    import ctypes
    # MB_ICONWARNING or MB_ICONINFORMATION, plus MB_SETFOREGROUND | MB_TOPMOST so it does not open behind other windows
    ctypes.windll.user32.MessageBoxW(None, text, TITLE, (0x30 if error else 0x40) | 0x10000 | 0x40000)


def run_gui(pkg, out, with_meta):
    import tkinter as tk
    from tkinter import font as tkfont, ttk
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:
        prog = Progress(uncompressed_size(pkg))
    except OSError as e:
        _msg("Could not unpack.\n%s\n\n%s" % (pkg, _plain(e)), error=True)
        return 1
    box = {"result": None, "error": None}

    def work():
        try:
            box["result"] = extract(pkg, out, with_meta, prog)
        except Cancelled:
            box["error"] = "cancelled"
        except Exception as e:
            box["error"] = e

    root = tk.Tk()
    root.title(TITLE)
    root.resizable(False, False)
    root.withdraw()
    font = ("Segoe UI", 9)
    frm = ttk.Frame(root, padding=12)
    frm.grid()
    ttk.Label(frm, text=os.path.basename(pkg), font=("Segoe UI", 9, "bold")).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
    cells = {}
    labels = [("Elapsed time:", "elapsed"), ("Total size:", "total"), ("Remaining time:", "remaining"),
              ("Speed:", "speed"), ("Files:", "files"), ("Processed:", "processed")]
    for i, (text, key) in enumerate(labels):
        r, c = 1 + i // 2, (i % 2) * 2
        ttk.Label(frm, text=text, font=font).grid(row=r, column=c, sticky="w", padx=(0, 8), pady=1)
        cells[key] = ttk.Label(frm, text="", font=font, width=13, anchor="e")
        cells[key].grid(row=r, column=c + 1, sticky="e", padx=(0, 18), pady=1)
    current = ttk.Label(frm, text="", font=font, width=64, anchor="w")
    current.grid(row=4, column=0, columnspan=4, sticky="w", pady=(10, 4))
    bar = ttk.Progressbar(frm, maximum=1000)
    bar.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(0, 10))
    pct = ttk.Label(frm, text="0%", font=font)
    pct.grid(row=6, column=0, sticky="w")
    measure = tkfont.Font(font=font).measure
    room = measure("0") * 63   # one character of slack, since kerning is ignored below
    widths = {}                # measuring a Japanese character takes a few ms, so each one is measured once
    shown = [None]

    def width(text):
        w = 0
        for ch in text:
            if ch not in widths:
                widths[ch] = measure(ch)
            w += widths[ch]
        return w

    def fit(text):
        """Cut the start of a path so its end, the file name, stays visible."""
        if width(text) <= room:
            return text
        w = width("…")
        for i in range(len(text) - 1, -1, -1):
            w += widths[text[i]]
            if w > room:
                return "…" + text[i + 1:]
        return text

    def cancel():
        prog.cancel = True
        btn.state(["disabled"])
        current.config(text="Cancelling…")

    btn = ttk.Button(frm, text="Cancel", command=cancel)
    btn.grid(row=6, column=3, sticky="e")
    root.protocol("WM_DELETE_WINDOW", cancel)

    worker = threading.Thread(target=work, daemon=True)
    worker.start()

    def tick():
        if not worker.is_alive():
            root.destroy()
            return
        el = time.time() - prog.started
        if el > 0.3 and root.state() == "withdrawn":
            root.deiconify()
            root.lift()
        cells["elapsed"].config(text=fmt_time(el))
        if not prog.cancel:
            frac = min(prog.done / prog.total, 1.0) if prog.total else 0
            speed = prog.done / el if el > 0 else 0
            remain = (prog.total - prog.done) / speed if speed > 0 else 0
            bar["value"] = int(frac * 1000)
            pct.config(text="%d%%" % int(frac * 100))
            cells["total"].config(text=fmt_size(prog.total))
            cells["remaining"].config(text=fmt_time(remain) if frac > 0.01 else "")
            cells["files"].config(text=str(prog.files))
            cells["speed"].config(text=fmt_size(speed) + "/s")
            cells["processed"].config(text=fmt_size(prog.done))
            if prog.current != shown[0]:
                shown[0] = prog.current
                current.config(text=fit(prog.current))
        root.after(100, tick)

    root.after(100, tick)
    root.mainloop()
    worker.join()
    err, r = box["error"], box["result"]
    if err == "cancelled":
        return 1
    if err is not None:
        _msg("Could not unpack.\n%s\n\n%s" % (pkg, _plain(err)), error=True)
        return 1
    notes = []
    if r["skipped_unsafe"]:
        notes.append("Unsafe paths: %d" % r["skipped_unsafe"])
    if r["duplicate_paths"]:
        notes.append("Paths that differ only in letter case: %d" % r["duplicate_paths"])
    if r["no_pathname"]:
        notes.append("Missing paths: %d" % r["no_pathname"])
    if notes:
        _msg("Unpacked, but some entries were skipped.\n%s\n\n%s" % (r["out"], "\n".join(notes)), error=True)
    return 0


MENU_KEY = r"Software\Classes\SystemFileAssociations\.unitypackage\shell\unitypackage_unpacker"


def menu_command():
    """The command Explorer runs for the right-click entry."""
    if getattr(sys, "frozen", False):
        return '"%s" "%%1"' % sys.executable
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return '"%s" "%s" "%%1"' % (pyw if os.path.exists(pyw) else exe, os.path.abspath(__file__))


def install():
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, MENU_KEY) as k:
        winreg.SetValueEx(k, None, 0, winreg.REG_SZ, MENU_LABEL)
        if getattr(sys, "frozen", False):
            winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, sys.executable)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, MENU_KEY + r"\command") as k:
        winreg.SetValueEx(k, None, 0, winreg.REG_SZ, menu_command())


def uninstall():
    import winreg
    for sub in (MENU_KEY + r"\command", MENU_KEY):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
        except FileNotFoundError:
            pass


def installed_command():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, MENU_KEY + r"\command") as k:
            return winreg.QueryValueEx(k, None)[0]
    except FileNotFoundError:
        return None


def run_setup_window():
    """Shown when the exe is opened without a file: install or remove the right-click entry."""
    import tkinter as tk
    from tkinter import ttk
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    root.title("%s %s" % (TITLE, __version__))
    root.resizable(False, False)
    frm = ttk.Frame(root, padding=14)
    frm.grid()
    ttk.Label(frm, text="Adds \"%s\" to the right-click menu of .unitypackage files." % MENU_LABEL,
              font=("Segoe UI", 9)).grid(row=0, column=0, columnspan=3, sticky="w")
    ttk.Label(frm, text="On Windows 11 it is under \"Show more options\" (or Shift + right-click).",
              font=("Segoe UI", 9)).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 10))
    status = ttk.Label(frm, text="", font=("Segoe UI", 9, "bold"))
    status.grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 10))

    def refresh():
        cur = installed_command()
        if cur is None:
            status.config(text="Status: not installed")
        elif cur == menu_command():
            status.config(text="Status: installed")
        else:
            status.config(text="Status: installed from another location (Install updates it)")

    def change(fn):
        try:
            fn()
        except OSError as e:
            status.config(text="Could not change the menu: %s" % e)
            return
        refresh()

    ttk.Button(frm, text="Install", command=lambda: change(install)).grid(row=3, column=0, sticky="w")
    ttk.Button(frm, text="Uninstall", command=lambda: change(uninstall)).grid(row=3, column=1, sticky="w", padx=8)
    ttk.Button(frm, text="Close", command=root.destroy).grid(row=3, column=2, sticky="e")
    refresh()
    root.mainloop()
    return 0


class _Parser(argparse.ArgumentParser):
    """Shows help, the version and usage errors in a message box when there is no console (the windowed exe)."""

    def _print_message(self, message, file=None):
        if sys.stdout is None:
            if message:
                _msg(message.strip())
        else:
            super()._print_message(message, file)

    def error(self, message):
        if sys.stdout is None:
            _msg("%s\n\n%s" % (message, self.format_usage().strip()), error=True)
            sys.exit(2)
        super().error(message)


def main():
    ap = _Parser(description="Unpack a .unitypackage into its original folder structure.")
    ap.add_argument("packages", nargs="*", metavar="package")
    ap.add_argument("--out")
    ap.add_argument("--no-meta", action="store_true", help="do not write .meta files")
    ap.add_argument("--gui", action="store_true", help="show the progress window")
    ap.add_argument("--install", action="store_true", help="add the right-click menu entry for the current user")
    ap.add_argument("--uninstall", action="store_true", help="remove the right-click menu entry")
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    a = ap.parse_args()
    console = sys.stdout is not None
    if a.install or a.uninstall:
        try:
            install() if a.install else uninstall()
        except OSError as e:
            text = "Could not change the right-click menu.\n%s" % e
            _msg(text, error=True) if not console else print(text, file=sys.stderr)
            return 1
        if console:
            print("installed: " + menu_command() if a.install else "uninstalled")
        return 0
    if not a.packages:
        if not console:
            return run_setup_window()
        ap.print_help()
        return 2
    if a.out and len(a.packages) > 1:
        ap.error("--out takes a single package")
    code = 0
    for pkg in a.packages:
        if a.gui or not console:
            code |= run_gui(pkg, a.out, not a.no_meta)
            continue
        try:
            r = extract(pkg, a.out, not a.no_meta)
        except Exception as e:
            print("error: %s: %s" % (pkg, _plain(e)), file=sys.stderr)
            code = 1
            continue
        print(json.dumps(r, ensure_ascii=False))
    return code


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
