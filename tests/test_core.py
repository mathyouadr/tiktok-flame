"""Pure logic: usernames, config, what is left to send, QR code helpers."""
import base64
import io
import tempfile
import unittest

from tiktok_flame import browser
from tiktok_flame.core import Store, clean_user, normalize_config, pending


class CoreTests(unittest.TestCase):
    def test_clean_user(self):
        self.assertEqual(clean_user("@Jane.Doe_"), "jane.doe_")
        self.assertEqual(clean_user("https://www.tiktok.com/@someone42?lang=en"), "someone42")
        for bad in ("a", "not a name", "x" * 25, "<script>", ""):
            self.assertIsNone(clean_user(bad))

    def test_normalize(self):
        cfg = normalize_config({"friends": [{"user": "@Bob"}, "bob", {"user": "eve", "on": False},
                                            {"user": "!!"}, {"user": "carl", "message": " yo "}],
                                "message": "  ", "webhook": "javascript:alert(1)"})
        self.assertEqual([f["user"] for f in cfg["friends"]], ["bob", "eve", "carl"])
        self.assertEqual(cfg["message"], "🔥")
        self.assertEqual(cfg["friends"][2]["message"], "yo")
        self.assertIsNone(cfg["webhook"])

    def test_pending(self):
        cfg = normalize_config({"friends": ["bob", {"user": "eve", "on": False}, {"user": "carl", "message": "yo"}]})
        st = {"sent": {"bob": "2026-01-02"}}
        self.assertEqual(pending(cfg, st, "2026-01-02"), [("carl", "yo")])
        self.assertEqual(pending(cfg, st, "2026-01-03"), [("bob", "🔥"), ("carl", "yo")])

    def test_store_and_lock(self):
        s = Store(tempfile.mkdtemp())
        s.save_config({"friends": ["bob"]})
        self.assertEqual(s.config()["friends"], [{"user": "bob", "on": True}])
        with s.lock("test") as ok:
            self.assertTrue(ok)
            self.assertEqual(s.state()["running"]["what"], "test")
            with s.lock("other") as ok2:      # second holder is refused
                self.assertFalse(ok2)
        self.assertIsNone(s.state()["running"])


class QrTests(unittest.TestCase):
    LINK = "https://www.tiktok.com/t/ZZTEST123/"

    def test_decode(self):
        import zxingcpp
        from PIL import Image
        bc = zxingcpp.create_barcode(self.LINK, zxingcpp.BarcodeFormat.QRCode)
        buf = io.BytesIO()
        Image.fromarray(zxingcpp.write_barcode_to_image(bc, scale=6)).save(buf, "PNG")
        self.assertEqual(browser.decode_qr("data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()), self.LINK)
        self.assertIsNone(browser.decode_qr("data:image/png;base64,AAAA"))

    def test_terminal_qr_is_scannable(self):
        """Turn the half-block text back into pixels the way a dark terminal shows it, then decode it."""
        import zxingcpp
        from PIL import Image
        for invert in (False, True):
            lines = browser.qr_terminal(self.LINK, invert=invert).split("\n")
            w, h = len(lines[0]), len(lines) * 2
            img = Image.new("L", (w, h))
            lit = {"█": (1, 1), "▀": (1, 0), "▄": (0, 1), " ": (0, 0)}
            for y, line in enumerate(lines):
                for x, ch in enumerate(line):
                    top, bottom = lit[ch]
                    img.putpixel((x, 2 * y), 255 * top)        # lit cell = light foreground
                    img.putpixel((x, 2 * y + 1), 255 * bottom)
            if invert:                                         # light theme: dark text on light background
                img = img.point(lambda v: 255 - v)
            img = img.resize((w * 8, h * 8), Image.NEAREST)
            self.assertEqual([r.text for r in zxingcpp.read_barcodes(img)], [self.LINK], f"invert={invert}")


if __name__ == "__main__":
    unittest.main()
