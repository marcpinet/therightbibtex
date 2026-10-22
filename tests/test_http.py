import support  # noqa: F401

import asyncio
import http.server
import threading
import unittest
from unittest import mock

import httpx

from therightbibtex.sources import http as http_mod
from therightbibtex.sources import pages
from therightbibtex.sources.base import Ctx
from therightbibtex.sources.http import Http, SourceError


class Client(unittest.TestCase):
    def test_timeouts_status_and_breaker(self):
        async def go():
            h = Http()
            seen = {}

            async def fake(method, url, **kw):
                seen["timeout"] = kw["timeout"]
                if url.endswith("/slow"):
                    await asyncio.sleep(5)
                return httpx.Response(404, request=httpx.Request(method, url))

            h.client.request = fake
            with self.assertRaises(SourceError) as cm:
                await h.request("src", "https://example.org/missing", timeout=10)
            self.assertEqual(cm.exception.status, 404)
            self.assertEqual((seen["timeout"].connect, seen["timeout"].read), (http_mod.CONNECT_TIMEOUT, 10))
            # Trois appels abandonnés après SLOW_CANCEL : la source est coupée.
            with mock.patch.object(http_mod, "SLOW_CANCEL", 0.05):
                for _ in range(3):
                    with self.assertRaises(asyncio.TimeoutError):
                        await asyncio.wait_for(h.request("lent", "https://example.org/slow", refresh=True), 0.2)
            with self.assertRaises(SourceError) as cm:
                await h.request("lent", "https://example.org/slow", refresh=True)
            self.assertEqual(cm.exception.kind, "unavailable")
            await h.close()
        asyncio.run(go())


class Ssrf(unittest.TestCase):
    def test_refuses_local_targets(self):
        for url in ("http://127.0.0.1/", "http://localhost/x", "http://10.0.0.1/", "http://192.168.1.15:8137/",
                    "http://[::1]/", "file:///etc/passwd", "http://1.1.1.1:22/"):
            with self.assertRaises(SourceError, msg=url):
                asyncio.run(pages._check_public(url))

    def test_accepts_public_address(self):
        self.assertEqual(asyncio.run(pages._check_public("https://1.1.1.1/")), "1.1.1.1")


class _Handler(http.server.BaseHTTPRequestHandler):
    BIG = b"<html><head><meta name='citation_title' content='Un titre de test'></head><body>" + b"x" * 5_000_000

    def do_GET(self):
        self.server.hosts.append(self.headers.get("Host"))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/page")
            self.end_headers()
            return
        body, ctype = {"/page": (self.BIG, "text/html"), "/paper.pdf": (b"%PDF-1.4\n" + b"0" * 2000, "application/pdf"),
                       "/huge.pdf": (b"%PDF-1.4\n" + b"0" * 300_000, "application/pdf")}[self.path]
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass   # le client a coupé (plafond atteint)

    def log_message(self, *a):
        pass


class Generic(unittest.TestCase):
    """Téléchargement d'une page quelconque : une seule lecture, plafonnée,
    connexion à l'adresse vérifiée en gardant le nom dans Host."""

    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.srv.hosts = []
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def fetch(self, path):
        async def go():
            h = Http()
            try:
                with mock.patch.object(pages, "_check_public", mock.AsyncMock(return_value="127.0.0.1")):
                    return await pages.generic(Ctx(h), f"http://revue.example:{self.port}{path}")
            finally:
                await h.close()
        return asyncio.run(go())

    def test_html_truncated_and_host_kept(self):
        self.srv.hosts.clear()
        rec, kind, data = self.fetch("/redirect")
        self.assertEqual((kind, data), ("html", b""))
        self.assertEqual(rec.title, "Un titre de test")
        self.assertEqual(rec.url, f"http://revue.example:{self.port}/page")
        self.assertEqual(self.srv.hosts, [f"revue.example:{self.port}"] * 2)

    def test_pdf_read_once(self):
        self.srv.hosts.clear()
        rec, kind, data = self.fetch("/paper.pdf")
        self.assertEqual((rec, kind), (None, "pdf"))
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertEqual(len(self.srv.hosts), 1)

    def test_pdf_too_big(self):
        with mock.patch.object(pages, "MAX_PDF", 100_000):
            with self.assertRaises(SourceError):
                self.fetch("/huge.pdf")


if __name__ == "__main__":
    unittest.main()
