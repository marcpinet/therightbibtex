import support  # noqa: F401

import asyncio
import json
import unittest
from datetime import date
from unittest import mock

from therightbibtex.sources import scholar
from therightbibtex.sources.base import Ctx, Query
from therightbibtex.sources.http import Http, Resp, SourceError, redact

SERP = json.loads((support.ROOT / "tests" / "fixtures" / "serpapi-scholar.json").read_text(encoding="utf-8"))
CAPTCHA = "<html><body><form id='gs_captcha_f'>unusual traffic</form></body></html>"


class FakeHttp(Http):
    """SerpAPI et Scholar simulés ; compte les appels par service."""

    def __init__(self, left=200):
        super().__init__()
        self.cache.conn.execute("DELETE FROM http_cache")   # chaque test part d'un cache vide
        self.left = left
        self.calls: list[str] = []

    async def request(self, source, url, *, params=None, **kw):
        self.calls.append(url.rsplit("/", 1)[-1])
        if url.endswith("account.json"):
            return Resp(200, json.dumps({"total_searches_left": self.left}), redact(url), {})
        if url.endswith("search.json"):
            return Resp(200, json.dumps(SERP), redact(url), {})
        return Resp(200, CAPTCHA, url, {})   # accès direct à Google : captcha


KEY = "cle-de-test"


def run(http, title="Attention is all you need", key=KEY, direct=False):
    async def go():
        try:
            ctx = Ctx(http, keys={"serpapi": key} if key else {}, scholar_direct=direct)
            return await scholar.search(ctx, Query(raw=title, title=title))
        finally:
            await http.close()
    return asyncio.run(go())


class FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 21)      # 11 jours jusqu'à la fin du mois : réserve de 15 + 3 x 11 = 48


@mock.patch.object(scholar, "date", FixedDate)
class SerpApi(unittest.TestCase):
    def setUp(self):
        scholar._used.clear()

    def test_parse(self):
        recs = run(FakeHttp())
        r = recs[0]
        self.assertEqual(r.title, "Attention is all you need")
        self.assertEqual([a.family for a in r.authors], ["Vaswani", "Shazeer", "Parmar"])
        self.assertTrue(r.incomplete_authors)
        self.assertEqual((r.year, r.citations, r.ids["scholar"]), (2017, 1234, "SYNTHETIC001"))
        self.assertTrue(r.venue_short.startswith("Advances in neural"))
        self.assertNotIn("src_trk", r.pdf_url)
        self.assertEqual(scholar._used[scholar._usage_key(KEY, FixedDate.today())], 1)

    def test_cached_answer_is_free(self):
        http = FakeHttp()
        params = {"engine": "google_scholar", "q": "Attention is all you need", "num": 10, "hl": "en",
                  "api_key": KEY}
        http.cache.put(http.cache_key("GET", f"{scholar.SERPAPI}/search.json", params, None, None),
                       Resp(200, json.dumps(SERP), "", {}), 60)
        self.assertTrue(run(http))
        self.assertNotIn("account.json", http.calls)      # ni vérification de quota...
        self.assertEqual(scholar._used, {})                # ...ni recherche décomptée

    def test_reserve_left_to_other_uses(self):
        http = FakeHttp(left=48)
        with self.assertRaises(SourceError) as cm:
            run(http)
        self.assertNotIn("search.json", http.calls)        # aucune recherche SerpAPI dépensée
        self.assertIn("SerpAPI quota", cm.exception.message)
        self.assertNotIn("scholar", http.calls)            # accès direct désactivé par défaut
        with self.assertRaises(SourceError) as cm:
            run(FakeHttp(left=48), direct=True)
        self.assertIn("captcha", cm.exception.message)     # permis : tenté en secours

    def test_daily_share(self):
        # 200 restantes, réserve 48 : part du jour (200 + n - 48) / 11, soit 15,2 recherches.
        scholar._used[scholar._usage_key(KEY, FixedDate.today())] = 16
        with self.assertRaises(SourceError):
            run(FakeHttp(left=200))
        scholar._used[scholar._usage_key(KEY, FixedDate.today())] = 3
        self.assertTrue(run(FakeHttp(left=200)))

    def test_empty_result_is_valid(self):
        global SERP
        saved, SERP = SERP, {"error": "Google hasn't returned any results for this query.", "organic_results": []}
        try:
            self.assertEqual(run(FakeHttp()), [])
        finally:
            SERP = saved


class PerKey(unittest.TestCase):
    def test_without_key(self):
        http = FakeHttp()
        with self.assertRaises(SourceError):          # ni clé ni accès direct : rien n'est tenté
            run(http, key="")
        self.assertEqual(http.calls, [])
        http = FakeHttp()
        with self.assertRaises(SourceError):          # accès direct permis : captcha
            run(http, key="", direct=True)
        self.assertEqual(http.calls, ["scholar"])     # SerpAPI jamais appelé sans clé

    def test_quota_counted_per_key(self):
        with mock.patch.object(scholar, "date", FixedDate):
            scholar._used.clear()
            run(FakeHttp(), key="cle-premiere")
            run(FakeHttp(), key="cle-seconde")
        self.assertEqual(sorted(scholar._used.values()), [1, 1])


class SeveralKeys(unittest.TestCase):
    """Plusieurs clés par service : la suivante prend le relais de celle qui échoue."""

    def test_serpapi_next_key_when_first_is_spent(self):
        class TwoKeys(FakeHttp):
            async def request(self, source, url, *, params=None, **kw):
                if url.endswith("account.json"):
                    left = 10 if params["api_key"] == "cle-epuisee" else 200   # 10 < réserve de 48
                    self.calls.append(f"account:{params['api_key']}")
                    return Resp(200, json.dumps({"total_searches_left": left}), redact(url), {})
                if url.endswith("search.json"):
                    self.calls.append(f"search:{params['api_key']}")
                return await super().request(source, url, params=params, **kw)

        with mock.patch.object(scholar, "date", FixedDate):
            scholar._used.clear()
            http = TwoKeys()
            self.assertTrue(run(http, key=["cle-epuisee", "cle-secours"]))
        self.assertEqual([c for c in http.calls if ":" in c],
                         ["account:cle-epuisee", "account:cle-secours", "search:cle-secours"])

    def test_s2_rejected_key_then_next(self):
        from therightbibtex.sources import s2

        class S2(Http):
            def __init__(self):
                super().__init__()
                self.cache.conn.execute("DELETE FROM http_cache")
                self.keys = []

            async def get_json(self, source, url, *, headers=None, **kw):
                key = (headers or {}).get("x-api-key", "")
                self.keys.append(key)
                if key == "cle-refusee":
                    raise SourceError("http", "HTTP 403", status=403)
                if key == "cle-saturee":
                    raise SourceError("ratelimited", "rate limited", status=429)
                return {"paperId": "abc", "title": "Attention is all you need"}

        async def go():
            http = S2()
            try:
                ctx = Ctx(http, keys={"s2": ["cle-refusee", "cle-saturee", "cle-valide"]})
                return await s2._call(ctx, "/paper/x"), http.keys
            finally:
                await http.close()

        s2._rejected.clear()
        data, tried = asyncio.run(go())
        self.assertEqual(data["paperId"], "abc")
        self.assertEqual(tried, ["cle-refusee", "cle-saturee", "cle-valide"])
        self.assertEqual(s2._rejected, {"cle-refusee"})   # écartée pour la suite ; la saturée reste utilisable
        s2._rejected.clear()

    def test_openalex_key_then_shared_budget(self):
        """Clé refusée (401) puis budget du jour épuisé (429) : on finit sur le budget partagé."""
        from therightbibtex.sources import openalex

        class OA(Http):
            def __init__(self):
                super().__init__()
                self.seen = []

            async def get_json(self, source, url, *, params=None, **kw):
                key = (params or {}).get("api_key", "")
                self.seen.append(key)
                if key == "cle-inconnue":
                    raise SourceError("http", "HTTP 401", status=401)
                if key == "cle-epuisee":
                    raise SourceError("ratelimited", "rate limited", status=429)
                return {"results": []}

        async def go():
            http = OA()
            try:
                ctx = Ctx(http, keys={"openalex": ["cle-inconnue", "cle-epuisee"]})
                return await openalex.get(ctx, openalex.API, {"search": "x"}), http.seen
            finally:
                await http.close()

        openalex._rejected.clear()
        data, seen = asyncio.run(go())
        self.assertEqual((data, seen), ({"results": []}, ["cle-inconnue", "cle-epuisee", ""]))
        self.assertEqual(openalex._rejected, {"cle-inconnue"})
        openalex._rejected.clear()


class Redaction(unittest.TestCase):
    def test_api_key_never_cached(self):
        self.assertEqual(redact("https://serpapi.com/search.json?engine=x&api_key=SECRET&q=y"),
                         "https://serpapi.com/search.json?engine=x&api_key=***&q=y")
        self.assertEqual(redact("https://example.org/?apikey=1"), "https://example.org/?apikey=***")
        self.assertEqual(redact("https://example.org/?monkey=1"), "https://example.org/?monkey=1")


if __name__ == "__main__":
    unittest.main()
