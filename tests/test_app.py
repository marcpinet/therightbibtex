import support  # noqa: F401

import json
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from therightbibtex import config, store
from therightbibtex import web as app_mod

SAMPLE = ("@inproceedings{vaswani2017attention, author = {Vaswani, Ashish}, title = {Attention is All you Need}, "
          "booktitle = {Advances in Neural Information Processing Systems}, year = {2017}}")
PREPRINT = ("@article{he2015deep, author = {He, Kaiming}, title = {Deep Residual Learning}, "
            "journal = {arXiv preprint arXiv:1512.03385}, year = {2015}}")
LOCAL = "http://localhost:8137"


def fresh():
    """Bibliothèque et configuration vides : chaque test part de zéro."""
    store.init()
    conn = store.connect()
    for table in ("favorites", "folders", "history"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()
    config.path().unlink(missing_ok=True)


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = TestClient(app_mod.app, base_url=LOCAL)
        cls.ctx.__enter__()          # lifespan : base, client HTTP, entretien quotidien

    @classmethod
    def tearDownClass(cls):
        cls.ctx.__exit__(None, None, None)

    def setUp(self):
        fresh()
        self.c = TestClient(app_mod.app, base_url=LOCAL)

    def test_headers(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)
        csp = r.headers["content-security-policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotIn("strict-transport-security", r.headers)     # serveur local en HTTP
        self.assertNotIn("fonts.googleapis.com", r.text)
        self.assertEqual(self.c.get("/static/fonts/bricolage-latin.woff2").headers["content-type"], "font/woff2")
        # Le favicon garde son <style> (thème du système), sans rien d'autre ; la page reste stricte.
        icon = self.c.get("/static/favicon.svg")
        self.assertEqual(icon.headers["content-security-policy"], app_mod.SVG_CSP)
        self.assertNotIn("unsafe-inline", csp)
        self.assertRegex(r.text, r'favicon\.svg\?v=[0-9a-f]{10}"')
        me = self.c.get("/api/me").json()
        self.assertEqual((me["authenticated"], me["home"]), (True, str(config.home())))

    def test_only_local_host_names(self):
        """DNS rebinding : une page qui ferait pointer son domaine vers 127.0.0.1 n'obtient rien."""
        for base in ("http://evil.example:8137", "http://192.168.1.20:8137", "http://localhost.evil.example"):
            self.assertEqual(TestClient(app_mod.app, base_url=base).get("/api/favorites").status_code, 421, base)
        for base in ("http://127.0.0.1:8137", "http://localhost"):
            self.assertEqual(TestClient(app_mod.app, base_url=base).get("/api/favorites").status_code, 200, base)
        # (le client de test ne lit pas une adresse IPv6 dans l'URL : en-tête Host direct)
        self.assertEqual(self.c.get("/api/favorites", headers={"Host": "[::1]:8137"}).status_code, 200)
        self.assertEqual([app_mod.host_name(h) for h in ("LocalHost:8137", "[::1]:8137", "127.0.0.1", "::1")],
                         ["localhost", "[::1]", "127.0.0.1", "::1"])
        with mock.patch.object(app_mod, "allowed_hosts", None):       # serve --host 0.0.0.0
            self.assertEqual(TestClient(app_mod.app, base_url="http://192.168.1.20:8137").get("/api/favorites")
                             .status_code, 200)

    def test_cross_site_refused(self):
        for headers in ({"Sec-Fetch-Site": "same-site"}, {"Sec-Fetch-Site": "cross-site"},
                        {"Origin": "https://evil.example"}, {"Origin": "null"}):
            r = self.c.post("/api/favorites/bulk", json={"action": "delete", "ids": [1]}, headers=headers)
            self.assertEqual(r.status_code, 403, headers)
        for headers in ({"Sec-Fetch-Site": "same-origin"}, {"Origin": LOCAL}, {}):
            r = self.c.post("/api/folders", json={"name": f"ok {len(headers)}"}, headers=headers)
            self.assertEqual(r.status_code, 200, headers)
        # Les lectures ne sont pas concernées.
        self.assertEqual(self.c.get("/api/folders", headers={"Sec-Fetch-Site": "cross-site"}).status_code, 200)

    def test_favorites_and_exports(self):
        c = self.c
        folder = c.post("/api/folders", json={"name": "Thèse"}).json()["id"]
        self.assertIn("id", c.post("/api/favorites", json={"folder_id": folder, "bibtex": SAMPLE}).json())
        self.assertIn("duplicate", c.post("/api/favorites", json={"folder_id": folder, "bibtex": SAMPLE}).json())
        c.post("/api/favorites", json={"folder_id": folder, "bibtex": SAMPLE, "force": True})
        c.post("/api/favorites", json={"folder_id": None, "bibtex": PREPRINT})
        bib = c.get(f"/api/export?folder={folder}")
        self.assertIn("vaswani2017attentiona", bib.text)      # clé en double suffixée
        self.assertIn("Th%C3%A8se.bib", bib.headers["content-disposition"])
        csl = c.get("/api/export?folder=all&format=csl")
        self.assertEqual(csl.headers["content-type"].split(";")[0], "application/vnd.citationstyles.csl+json")
        items = csl.json()
        self.assertEqual(len(items), 3)
        self.assertEqual({i["type"] for i in items}, {"paper-conference", "article"})
        favs = c.get("/api/favorites?folder=all").json()
        self.assertFalse(favs["truncated"])
        with mock.patch.object(app_mod, "LIST_LIMIT", 2):
            favs = c.get("/api/favorites?folder=all").json()
        self.assertEqual((len(favs["favorites"]), favs["truncated"]), (2, True))
        self.assertEqual(len(c.get("/api/export?folder=all&format=csl").json()), 3)   # export complet
        # corbeille et annulation
        ids = [f["id"] for f in c.get("/api/favorites?folder=all").json()["favorites"]]
        token = c.post("/api/favorites/bulk", json={"action": "delete", "ids": ids}).json()["undo"]
        self.assertEqual(c.get("/api/favorites?folder=all").json()["favorites"], [])
        self.assertEqual(c.post("/api/undo", json={"token": token}).json()["restored"], 3)

    def test_two_searches_at_most(self):
        app_mod._active[store.LOCAL_UID] = 2
        try:
            r = self.c.post("/api/search", json={"q": "Attention is all you need"})
        finally:
            app_mod._active[store.LOCAL_UID] = 0
        self.assertEqual((r.status_code, r.headers["retry-after"]), (429, "3"))

    def test_folder_colors_and_nesting(self):
        c = self.c
        these = c.post("/api/folders", json={"name": "Thèse", "color": "blue"}).json()["id"]
        ch = c.post("/api/folders", json={"name": "Chapitre 1", "parent_id": these}).json()["id"]          # hérite du bleu
        plain = c.post("/api/folders", json={"name": "Lectures"}).json()["id"]
        self.assertEqual(c.post("/api/folders", json={"name": "X", "color": "fuchsia"}).status_code, 400)
        colors = {f["name"]: (f["color"], f["parent_id"]) for f in c.get("/api/folders").json()["folders"]}
        self.assertEqual(colors, {"Thèse": ("blue", None), "Chapitre 1": ("blue", these), "Lectures": ("", None)})
        self.assertEqual(c.patch(f"/api/folders/{ch}", json={"color": "pink"}).status_code, 200)
        self.assertEqual(c.patch(f"/api/folders/{plain}", json={"parent_id": ch}).status_code, 200)       # glisser-déposer
        self.assertEqual(c.patch(f"/api/folders/{these}", json={"parent_id": plain}).status_code, 400)    # dans son descendant
        self.assertEqual(c.patch(f"/api/folders/{ch}", json={"color": "rouge"}).status_code, 400)
        colors = {f["name"]: (f["color"], f["parent_id"]) for f in c.get("/api/folders").json()["folders"]}
        self.assertEqual(colors["Chapitre 1"], ("pink", these))
        self.assertEqual(colors["Lectures"][1], ch)

    def test_api_keys_in_config_file(self):
        c = self.c
        quota = mock.AsyncMock(return_value={"total_searches_left": 123})
        with mock.patch.object(app_mod, "_check_key", mock.AsyncMock(return_value="")), \
                mock.patch.object(app_mod.http, "get_json", quota):
            r = c.post("/api/keys/serpapi", json={"value": "cle-serpapi-0123456789"})
            self.assertEqual(r.status_code, 200)
            self.assertNotIn("cle-serpapi-0123456789", r.text)              # jamais renvoyée en entier
            k = r.json()["keys"]
            self.assertEqual([(x["hint"], x["quota"]) for x in k["serpapi"]], [("…6789", 123)])
            self.assertEqual(k["s2"], [])
            # Deuxième clé : ajoutée après la première, essayée si celle-ci échoue.
            k = c.post("/api/keys/serpapi", json={"value": "cle-secours-9876543210"}).json()["keys"]
            self.assertEqual([x["hint"] for x in k["serpapi"]], ["…6789", "…3210"])
            c.post("/api/keys/serpapi", json={"value": "cle-serpapi-0123456789"})   # déjà là : rien ne change
            self.assertEqual(len(c.get("/api/keys").json()["keys"]["serpapi"]), 2)
            self.assertEqual(c.post("/api/keys/serpapi", json={"value": "pas une clé !"}).status_code, 400)
            self.assertEqual(c.post("/api/keys/inconnu", json={"value": "cle-serpapi-0123456789"}).status_code, 404)
        with mock.patch.object(app_mod, "_check_key", mock.AsyncMock(return_value="SerpAPI rejects this key")):
            r = c.post("/api/keys/s2", json={"value": "cle-refusee-0123456789"})
            self.assertEqual((r.status_code, r.json()["error"]), (400, "SerpAPI rejects this key"))
        saved = json.loads(config.path().read_text(encoding="utf-8"))
        self.assertEqual(saved["keys"], {"serpapi": ["cle-serpapi-0123456789", "cle-secours-9876543210"]})
        if os.name == "posix":
            self.assertEqual(config.path().stat().st_mode & 0o777, 0o600)    # lisible par son propriétaire seul
        self.assertEqual(c.delete("/api/keys/serpapi/7").status_code, 404)
        with mock.patch.object(app_mod.http, "get_json", quota):
            self.assertEqual([x["hint"] for x in c.delete("/api/keys/serpapi/0").json()["keys"]["serpapi"]], ["…3210"])
        self.assertEqual(config.get_api_keys(), {"serpapi": ["cle-secours-9876543210"]})

    def test_settings_merge_and_card_width(self):
        """La poignée de la notice et la fenêtre des réglages enregistrent chacune leurs champs
        sans effacer ceux de l'autre ; la largeur est bornée, 0 = largeur par défaut."""
        c = self.c
        self.assertEqual(c.put("/api/settings", json={"card_width": 1240}).json()["card_width"], 1240)
        s = c.put("/api/settings", json={"key_style": "dblp"}).json()
        self.assertEqual((s["card_width"], s["key_style"]), (1240, "dblp"))
        self.assertEqual(c.get("/api/me").json()["settings"]["card_width"], 1240)
        self.assertEqual(c.put("/api/settings", json={"card_width": 50}).json()["card_width"], 480)
        self.assertEqual(c.put("/api/settings", json={"card_width": 0}).json()["card_width"], 0)
        self.assertEqual(c.put("/api/settings", json={"card_width": "large"}).json()["key_style"], "dblp")
        self.assertEqual(json.loads(config.path().read_text(encoding="utf-8"))["settings"]["key_style"], "dblp")

    def test_search_uses_the_config(self):
        config.add_api_key("serpapi", "cle-de-la-config")
        seen = []

        class FakeSearch:
            def __init__(self, http, q, refresh=False, settings=None, keys=None, scholar_direct=False):
                seen.append((keys, scholar_direct, settings["key_style"]))

            async def stream(self):
                yield {"type": "done", "elapsed": 0}

        with mock.patch.object(app_mod.engine, "Search", FakeSearch):
            self.c.post("/api/search", json={"q": "Attention is all you need"})
            config.set_option("scholar_direct", True)
            config.save_settings({"key_style": "AuthorYear"})
            self.c.post("/api/search", json={"q": "Attention is all you need"})
        self.assertEqual(seen, [({"serpapi": ["cle-de-la-config"]}, False, "scholar"),
                                ({"serpapi": ["cle-de-la-config"]}, True, "AuthorYear")])

    def test_housekeeping_backup(self):
        for old in store.BACKUP_DIR.glob("library-*.db"):   # copie faite au démarrage, base encore vide
            old.unlink()
        store.housekeeping()
        backups = list(store.BACKUP_DIR.glob("library-????-??-??.db"))
        self.assertEqual(len(backups), 1)
        store.housekeeping()                               # une seule copie par jour
        self.assertEqual(len(list(store.BACKUP_DIR.glob("library-*.db"))), 1)
        import sqlite3
        conn = sqlite3.connect(str(backups[0]))
        self.assertEqual(conn.execute("SELECT name FROM users").fetchall(), [("local",)])
        conn.close()


class Config(unittest.TestCase):
    def setUp(self):
        fresh()

    def test_defaults_and_options(self):
        cfg = config.load()
        self.assertEqual((cfg["port"], cfg["scholar_direct"], cfg["keys"]), (config.DEFAULT_PORT, False, {}))
        config.set_option("contact_email", "someone@example.org")
        self.assertEqual(config.get_option("contact_email"), "someone@example.org")
        with self.assertRaises(config.ConfigError):
            config.set_option("port", "8000")
        with self.assertRaises(config.ConfigError):
            config.set_option("nope", 1)

    def test_broken_file_is_reported_not_overwritten(self):
        config.path().parent.mkdir(parents=True, exist_ok=True)
        config.path().write_text("{ not json", encoding="utf-8")
        with self.assertRaises(config.ConfigError):
            config.load()
        with self.assertRaises(config.ConfigError):
            config.add_api_key("s2", "cle-s2-0123456789")
        self.assertEqual(config.path().read_text(encoding="utf-8"), "{ not json")

    def test_keys(self):
        self.assertIsNone(config.add_api_key("s2", "cle-s2-0123456789"))
        self.assertIsNone(config.add_api_key("s2", "cle-s2-0123456789"))         # déjà là
        self.assertEqual(config.add_api_key("s2", "pas une clé"), "Invalid key (letters, digits, _ . -; 8 to 200 characters)")
        self.assertEqual(config.add_api_key("inconnu", "cle-0123456789"), "Unknown service")
        for i in range(config.MAX_KEYS_PER_SERVICE - 1):
            self.assertIsNone(config.add_api_key("s2", f"cle-supplementaire-{i}"))
        self.assertIn("At most", config.add_api_key("s2", "une-de-trop-0123"))
        self.assertTrue(config.delete_api_key("s2", 0))
        self.assertFalse(config.delete_api_key("s2", 99))
        self.assertEqual(config.api_keys_public()["s2"][0], {"id": 0, "hint": "…re-0"})   # cle-supplementaire-0


if __name__ == "__main__":
    unittest.main()
