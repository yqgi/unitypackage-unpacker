"""Tests for unitypackage_unpacker. Run: python -m unittest discover -s tests"""
import gzip, io, os, shutil, subprocess, sys, tarfile, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import unitypackage_unpacker as u  # noqa: E402


def entry(tf, name, data, kind=tarfile.REGTYPE):
    ti = tarfile.TarInfo(name)
    ti.size, ti.type = len(data), kind
    tf.addfile(ti, io.BytesIO(data))


def package(assets, order=("asset", "asset.meta", "pathname"), prefix="", fmt=tarfile.GNU_FORMAT):
    """assets: list of (pathname, data) or (guid, pathname, data)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=fmt) as tf:
        for i, a in enumerate(assets):
            guid, path, data = a if len(a) == 3 else ("%032x" % (i + 1), a[0], a[1])
            parts = {"asset": data, "asset.meta": b"guid: " + guid.encode() + b"\n",
                     "pathname": path.encode("utf-8") + b"\n00"}
            for k in order:
                entry(tf, prefix + guid + "/" + k, parts[k])
    return buf.getvalue()


def files_in(root):
    out = {}
    for base, _, names in os.walk(root):
        for n in names:
            p = os.path.join(base, n)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root).replace(os.sep, "/")] = fh.read()
    return out


class UnpackTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="upk-test-")

    def tearDown(self):
        shutil.rmtree(u._long(self.dir), ignore_errors=True)

    def write(self, data, name="テスト.unitypackage", gz=True):
        p = os.path.join(self.dir, name)
        with open(p, "wb") as f:
            f.write(gzip.compress(data) if gz else data)
        return p

    def unpack(self, data, gz=True, **kw):
        r = u.extract(self.write(data, gz=gz), **kw)
        return r, files_in(r["out"])

    def test_japanese_names_and_meta(self):
        r, f = self.unpack(package([("Assets/服/跋扈/テクスチャ.png", b"a" * 100)]))
        self.assertEqual(f["Assets/服/跋扈/テクスチャ.png"], b"a" * 100)
        self.assertIn("Assets/服/跋扈/テクスチャ.png.meta", f)
        self.assertEqual(os.path.basename(r["out"]), "テスト")

    def test_no_meta(self):
        r, f = self.unpack(package([("Assets/a.txt", b"x")]), with_meta=False)
        self.assertEqual(list(f), ["Assets/a.txt"])

    def test_orders_and_large_assets(self):
        big = os.urandom(u.CHUNK + 12345)
        for order in (("asset", "asset.meta", "pathname"), ("pathname", "asset", "asset.meta"),
                      ("asset", "pathname", "asset.meta")):
            r, f = self.unpack(package([("Assets/big.bin", big), ("Assets/s.txt", b"s")], order=order))
            self.assertEqual(f["Assets/big.bin"], big, order)
            self.assertEqual(f["Assets/s.txt"], b"s", order)
            self.assertEqual(r["files"], 2)
            self.assertFalse([k for k in f if ".unpack_tmp" in k])

    def test_plain_tar_and_dot_slash(self):
        r, f = self.unpack(package([("Assets/P/q.txt", b"2")], prefix="./"), gz=False)
        self.assertEqual(f, {"Assets/P/q.txt": b"2", "Assets/P/q.txt.meta": f["Assets/P/q.txt.meta"]})
        self.assertEqual(r["decoder"], "none")

    def test_pax_names(self):
        long = "Assets/" + "長い名前" * 40 + ".txt"
        r, f = self.unpack(package([(long, b"p")], fmt=tarfile.PAX_FORMAT))
        self.assertEqual(f[long], b"p")

    def test_unsafe_paths_are_skipped(self):
        r, f = self.unpack(package([("../evil.txt", b"1"), ("C:/evil2.txt", b"2"), ("/abs.txt", b"3"),
                                    (".unpack_tmp-1/x", b"4"), ("Assets/ok.txt", b"5")]))
        self.assertEqual(r["skipped_unsafe"], 4)
        self.assertEqual(sorted(f), ["Assets/ok.txt", "Assets/ok.txt.meta"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "evil.txt")))

    def test_reserved_names(self):
        r, f = self.unpack(package([("Assets/con.txt", b"1"), ("Assets/CON .png", b"2"), ("Assets/COM\u00b9", b"3"),
                                    ("Assets/nul", b"4")]))
        self.assertEqual(sorted(k for k in f if not k.endswith(".meta")),
                         sorted(["Assets/_CON .png", "Assets/_COM\u00b9", "Assets/_con.txt", "Assets/_nul"]))

    def test_case_duplicates_and_repeated_pathname(self):
        r, f = self.unpack(package([("Assets/Tex/Foo.png", b"first"), ("Assets/Tex/foo.png", b"second")]))
        self.assertEqual(r["duplicate_paths"], 1)
        self.assertEqual(f["Assets/Tex/Foo.png"], b"first")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            g = "a" * 32
            entry(tf, g + "/asset", b"x")
            entry(tf, g + "/pathname", b"Assets/first.txt")
            entry(tf, g + "/pathname", b"Assets/second.txt")
            entry(tf, g + "/asset.meta", b"m")
        r, f = self.unpack(buf.getvalue())
        self.assertEqual(sorted(f), ["Assets/first.txt", "Assets/first.txt.meta"])

    def test_hostile_guid_names_stay_inside(self):
        for guid in ("D:x", "a<b", "COM1", "x" * 300):
            r, f = self.unpack(package([(guid, "Assets/g.txt", b"g")]))
            self.assertEqual(f["Assets/g.txt"], b"g", guid)

    def test_folders(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            entry(tf, "f" * 32 + "/pathname", b"Assets/Empty")
            entry(tf, "f" * 32 + "/asset.meta", b"folderAsset: yes")
        r, f = self.unpack(buf.getvalue())
        self.assertEqual(r["folders"], 1)
        self.assertTrue(os.path.isdir(os.path.join(r["out"], "Assets", "Empty")))
        self.assertIn("Assets/Empty.meta", f)

    def test_broken_inputs_fail_and_clean_up(self):
        good = package([("Assets/a.txt", b"a" * 5000), ("Assets/b.txt", b"b" * 5000)])
        cases = {
            "html": (b"<html>not found</html>", False),
            "garbage": (os.urandom(4096), False),
            "cut in header": (good[:512 * 4 + 100], True),
            "no end marker": (good[:-1024].rstrip(b"\0") or good[:2048], True),
            "negative size": (good[:124] + b"-0000000001\0" + good[136:], True),
            "empty tar": (b"\0" * 1024, True),
        }
        for label, (data, gz) in cases.items():
            p = self.write(data, name="x.unitypackage", gz=gz)
            with self.assertRaises((ValueError, EOFError, OSError), msg=label):
                u.extract(p)
            self.assertFalse(os.path.exists(os.path.join(self.dir, "x")), label)

    def test_oversized_metadata_record(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
            entry(tf, "././@LongLink", b"a" * (u.MAX_META + 1), kind=tarfile.GNUTYPE_LONGNAME)
        with self.assertRaises(ValueError):
            u.extract(self.write(buf.getvalue(), name="x.unitypackage"))

    def test_malformed_pax_record_is_ignored(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            entry(tf, "pax", b"=x y\n", kind=tarfile.XHDTYPE)
            g = "b" * 32
            entry(tf, g + "/asset", b"v")
            entry(tf, g + "/pathname", b"Assets/v.txt")
        r, f = self.unpack(buf.getvalue())
        self.assertEqual(f["Assets/v.txt"], b"v")

    def test_cancel_into_existing_folder_leaves_no_partial_file(self):
        big = os.urandom(3 * u.CHUNK)
        p = self.write(package([("Assets/big.bin", big)], order=("pathname", "asset", "asset.meta")))
        out = os.path.join(self.dir, "existing")
        os.makedirs(out)
        prog = u.Progress(u.uncompressed_size(p))
        real = u._Writer.put

        def put(self, *op):
            if op[0] == "write":
                prog.cancel = True
            return real(self, *op)

        u._Writer.put = put
        try:
            with self.assertRaises(u.Cancelled):
                u.extract(p, out, progress=prog)
        finally:
            u._Writer.put = real
        self.assertEqual(files_in(out), {})
        self.assertTrue(os.path.isdir(out))

    def test_unique_output_folders(self):
        p = self.write(package([("Assets/a.txt", b"a")]))
        a, b = u.extract(p)["out"], u.extract(p)["out"]
        self.assertEqual(os.path.basename(b), os.path.basename(a) + " (2)")

    def test_long_output_path(self):
        out = os.path.join(self.dir, *(["d" * 60] * 4))
        deep = "Assets/" + "/".join(["e" * 40] * 3) + "/file.txt"
        r = u.extract(self.write(package([(deep, b"deep")])), out)
        target = os.path.join(u._long(out), *deep.split("/"))
        self.assertGreater(len(target), 300)
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), b"deep")

    def test_trailing_bytes_after_gzip_are_ignored(self):
        data = gzip.compress(package([("Assets/t.txt", b"t")])) + b"JUNKJUNK"
        for decoder in ("rapidgzip", "gzip"):
            saved = sys.modules.get("rapidgzip")
            if decoder == "gzip":
                sys.modules["rapidgzip"] = None   # makes the import fail, so the standard library is used
            try:
                r, f = self.unpack(data, gz=False)
            finally:
                if decoder == "gzip":
                    if saved is None:
                        sys.modules.pop("rapidgzip", None)
                    else:
                        sys.modules["rapidgzip"] = saved
            self.assertEqual(f["Assets/t.txt"], b"t", decoder)
            self.assertTrue(r["decoder"].startswith(decoder), r["decoder"])
            shutil.rmtree(r["out"])

    def test_multi_member_size_estimate(self):
        p = os.path.join(self.dir, "m.unitypackage")
        with open(p, "wb") as f:
            f.write(gzip.compress(os.urandom(6 * 1024 * 1024)) + gzip.compress(b"x" * 1000))
        self.assertLess(u.uncompressed_size(p), 1 << 30)

    def test_truncated_gzip_never_reports_success(self):
        data = gzip.compress(package([("Assets/%d.bin" % i, os.urandom(200000)) for i in range(10)]))
        code = ("import sys; sys.path.insert(0, %r); import unitypackage_unpacker as u\n"
                "try:\n    u.extract(sys.argv[1], sys.argv[2])\nexcept Exception as e:\n    sys.exit(3)\n"
                % os.path.dirname(HERE))
        for frac in (0.1, 0.5, 0.9, 0.999):
            p = os.path.join(self.dir, "t.unitypackage")
            with open(p, "wb") as f:
                f.write(data[:int(len(data) * frac)])
            out = os.path.join(self.dir, "t_out")
            rc = subprocess.run([sys.executable, "-c", code, p, out], capture_output=True).returncode
            self.assertNotEqual(rc, 0, frac)
            shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
