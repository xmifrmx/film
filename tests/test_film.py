import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import filmrss as f  # noqa: E402

NS = {"c": "http://purl.org/rss/1.0/modules/content/", "m": "http://search.yahoo.com/mrss/"}
OVERVIEW = ("Ben Sana İnanıyorum, işgüzar avukat Mahsun'un beklediği saygın kariyerin aksine, kendisini bir anda "
            "yeraltı dünyasının karmaşasında bulmasını konu alıyor.\n\nLevent Ülgen tarafından canlandırılan Mümin "
            "karakteri, tüm varlığını kaybetmiş eski bir figürdür.")


def detail(mid, **kw):
    d = {
        "id": mid, "title": f"Film {mid}", "original_title": f"Film {mid}", "overview": OVERVIEW,
        "poster_path": f"/p{mid}.jpg", "release_date": "2026-11-13", "runtime": 103,
        "genres": [{"id": 35, "name": "Komedi"}], "production_countries": [{"iso_3166_1": "TR", "name": "Turkey"}],
        "tagline": "", "adult": False,
        "release_dates": {"results": [{"iso_3166_1": "TR", "release_dates": [{"type": 3, "release_date": "2026-11-13T00:00:00.000Z"}]}]},
        "credits": {"cast": [{"name": "Mahsun Karaca", "character": "Mahsun", "order": 0, "profile_path": "/a.jpg"},
                             {"name": "Levent Ülgen", "character": "Mümin", "order": 1, "profile_path": None}],
                    "crew": [{"name": "Ömer Faruk Sorak", "job": "Director"},
                             {"name": "Cihan Talay", "job": "Screenplay"}, {"name": "Beste Bilir", "job": "Writer"}]},
        "videos": {"results": [{"site": "YouTube", "key": "abcDEF12345", "type": "Trailer", "official": True,
                                "iso_639_1": "tr", "published_at": "2026-10-01T00:00:00.000Z"}]},
    }
    d.update(kw)
    return d


class FakeApi:
    def __init__(self, lists, details):
        self.lists, self.details, self.detail_calls = lists, details, []

    def movie_list(self, kind, page=1):
        if isinstance(self.lists.get(kind), Exception):
            raise self.lists[kind]
        return {"results": [{"id": i, "adult": False} for i in self.lists.get(kind, [])], "total_pages": 1}

    def movie_detail(self, mid):
        self.detail_calls.append(mid)
        v = self.details[mid]
        if isinstance(v, Exception):
            raise v
        return v


NOW = datetime(2026, 11, 21, 6, 0, 0, tzinfo=timezone.utc)


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.log = []

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_it(self, api, now=NOW, **kw):
        return f.run(root=self.root, api=api, now=now, log=self.log.append, **kw)

    def items(self):
        root = ET.parse(os.path.join(self.root, "rss.xml")).getroot()
        return root, root.findall("channel/item")


class TestFormat(unittest.TestCase):
    def test_runtime_and_date(self):
        self.assertEqual(f.tr_runtime(103), "1 saat 43 dakika")
        self.assertEqual(f.tr_runtime(45), "45 dakika")
        self.assertEqual(f.tr_runtime(120), "2 saat")
        self.assertEqual(f.tr_runtime(0), "")
        self.assertEqual(f.tr_runtime(None), "")
        self.assertEqual(f.tr_date("2026-11-13"), "13 Kasım 2026")
        self.assertEqual(f.tr_date(""), "")

    def test_release_date_prefers_turkey(self):
        d = detail(1, release_date="2026-01-01")
        self.assertEqual(f.release_date(d), "2026-11-13")
        self.assertEqual(f.release_date(detail(1, release_dates={}, release_date="2026-05-05")), "2026-05-05")

    def test_trailer_preference(self):
        vids = [{"site": "YouTube", "key": "enTrailer01", "type": "Trailer", "official": True, "iso_639_1": "en"},
                {"site": "YouTube", "key": "trTeaser001", "type": "Teaser", "official": True, "iso_639_1": "tr"},
                {"site": "Vimeo", "key": "vimeoKey001", "type": "Trailer", "iso_639_1": "tr"},
                {"site": "YouTube", "key": "bad key!", "type": "Trailer", "iso_639_1": "tr"}]
        self.assertEqual(f.pick_trailer({"videos": {"results": vids}})["key"], "trTeaser001")
        self.assertIsNone(f.pick_trailer({"videos": {"results": vids[2:]}}))

    def test_shorten(self):
        s = f.shorten("kelime " * 100)
        self.assertLessEqual(len(s), 155)
        self.assertTrue(s.endswith("…"))


class TestArticle(unittest.TestCase):
    def test_sections_and_content(self):
        rec, body = f.build_article(detail(7, title="Av. Mahsun Karaca: Sana İnanıyorum", tagline="Slogan <x>"),
                                    "now_playing", NOW)
        self.assertTrue(body.lstrip().startswith('<div class="separator"'))
        for h in ("Film Bilgileri</h2>", "Konusu</h2>", "Oyuncuları</h2>"):
            self.assertIn(h, body)
        self.assertNotIn("<h1", body.lower())
        self.assertNotIn("Fragmanı</h2>", body)
        self.assertIn("https://www.youtube.com/embed/abcDEF12345", body)
        self.assertIn("https://www.youtube.com/watch?v=abcDEF12345", body)
        # video, Konusu bölümünün içinde (Oyuncuları başlığından önce, Konusu başlığından sonra)
        self.assertLess(body.index("Konusu</h2>"), body.index("<iframe"))
        self.assertLess(body.index("<iframe"), body.index("Oyuncuları</h2>"))
        self.assertIn("1 saat 43 dakika", body)
        self.assertIn("Türkiye", body)
        self.assertIn("13 Kasım 2026", body)
        self.assertIn("Ömer Faruk Sorak", body)
        self.assertIn("Cihan Talay, Beste Bilir", body)
        self.assertIn("Levent Ülgen", body)
        self.assertEqual(body.count("<p>Ben Sana") + body.count("<p>Levent Ülgen tarafından"), 2)   # iki ayrı paragraf
        self.assertIn("Slogan &lt;x&gt;", body)                                                    # kaçışlı
        for banned in ("Puan Ver", "İzledim", "Favorilerim", "Yorum"):
            self.assertNotIn(banned, body)
        self.assertIn("TMDB", body)
        self.assertEqual(rec["gaps"], [])
        self.assertEqual(rec["id"], "film-7")
        self.assertEqual(rec["link"], "https://www.themoviedb.org/movie/7")
        self.assertLessEqual(len(rec["description"]), 155)

    def test_rejections(self):
        self.assertEqual(f.build_article(detail(1, overview="kısa"), "now_playing", NOW), (None, "konu_kisa"))
        self.assertEqual(f.build_article(detail(1, poster_path=None), "now_playing", NOW), (None, "afis_yok"))
        self.assertEqual(f.build_article(detail(1, title=" "), "now_playing", NOW), (None, "bos_ad"))

    def test_gaps(self):
        rec, body = f.build_article(detail(1, videos={"results": []}, credits={"cast": [], "crew": []}), "upcoming", NOW)
        self.assertEqual(sorted(rec["gaps"]), ["fragman", "oyuncu"])
        self.assertNotIn("<iframe", body)
        self.assertNotIn("youtube.com", body)

    def test_thumb_mode(self):
        old = f.VIDEO_MODE
        f.VIDEO_MODE = "thumb"
        try:
            _, body = f.build_article(detail(1), "now_playing", NOW)
        finally:
            f.VIDEO_MODE = old
        self.assertNotIn("<iframe", body)
        self.assertIn("https://img.youtube.com/vi/abcDEF12345/hqdefault.jpg", body)
        self.assertIn("https://www.youtube.com/watch?v=abcDEF12345", body)

    def test_cast_without_photo_and_limit(self):
        cast = [{"name": f"Oyuncu {i}", "character": "", "order": i, "profile_path": None} for i in range(30)]
        _, body = f.build_article(detail(1, credits={"cast": cast, "crew": []}), "now_playing", NOW)
        self.assertEqual(body.count("<b>Oyuncu "), f.CAST_LIMIT)


class TestPipeline(Base):
    def test_generates_valid_rss_only(self):
        api = FakeApi({"now_playing": [1, 2], "upcoming": [3, 2]}, {i: detail(i, title=f"A & B {i} ]]> ") for i in (1, 2, 3)})
        res = self.run_it(api)
        self.assertEqual((res["new"], res["in_feed"]), (3, 3))
        root, items = self.items()
        self.assertEqual(len(items), 3)
        self.assertEqual(len({i.find("guid").text for i in items}), 3)
        self.assertEqual(root.find("channel/{http://www.w3.org/2005/Atom}link").attrib["href"], f.SITE_URL + "/rss.xml")
        for it in items:
            self.assertTrue(it.find("link").text.startswith("https://www.themoviedb.org/movie/"))
            self.assertIn("<h2>", it.find("c:encoded", NS).text)
            self.assertTrue(it.find("m:content", NS).attrib["url"].startswith("https://image.tmdb.org/"))
            self.assertLessEqual(len(it.find("description").text), 155)
        self.assertFalse(os.path.exists(os.path.join(self.root, "sitemap.xml")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "feed")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "makale")))

    def test_never_regenerates_known_film(self):
        api = FakeApi({"now_playing": [1, 2]}, {i: detail(i) for i in (1, 2)})
        self.run_it(api)
        api2 = FakeApi({"now_playing": [1, 2, 4]}, {i: detail(i) for i in (1, 2, 4)})
        res = self.run_it(api2, now=datetime(2026, 11, 21, 14, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(api2.detail_calls, [4])
        self.assertEqual(res["new"], 1)
        _, items = self.items()
        self.assertEqual(len(items), 3)

    def test_skip_then_retry_then_stop(self):
        api = FakeApi({"now_playing": [5]}, {5: detail(5, overview="kısa")})
        self.run_it(api)
        self.assertEqual(json.load(open(os.path.join(self.root, "data", "index.json")))["skips"]["5"]["n"], 1)
        api.details[5] = detail(5)                                   # konu sonradan eklendi
        res = self.run_it(api)
        self.assertEqual(res["new"], 1)
        self.assertNotIn("5", json.load(open(os.path.join(self.root, "data", "index.json")))["skips"])
        # sürekli kısa kalan film en fazla MAX_SKIP_ATTEMPTS kez denenir
        api2 = FakeApi({"now_playing": [9, 1]}, {9: detail(9, overview="kısa"), 1: detail(1)})
        for _ in range(f.MAX_SKIP_ATTEMPTS + 3):
            self.run_it(api2)
        self.assertEqual(api2.detail_calls.count(9), f.MAX_SKIP_ATTEMPTS)

    def test_gap_completion_keeps_identity(self):
        d0 = detail(6, videos={"results": []})
        api = FakeApi({"now_playing": [6]}, {6: d0})
        self.run_it(api)
        _, items = self.items()
        guid, pub = items[0].find("guid").text, items[0].find("pubDate").text
        api.details[6] = detail(6)                                   # fragman yayınlandı
        res = self.run_it(api, now=datetime(2026, 11, 22, 6, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(res["updated"], 1)
        _, items = self.items()
        self.assertEqual((items[0].find("guid").text, items[0].find("pubDate").text), (guid, pub))
        self.assertIn("youtube.com/embed", items[0].find("c:encoded", NS).text)
        calls = len(api.detail_calls)
        self.run_it(api, now=datetime(2026, 11, 23, 6, 0, 0, tzinfo=timezone.utc))      # eksik kalmadı: yeniden denenmez
        self.assertEqual(len(api.detail_calls), calls)

    def test_api_errors_do_not_kill_run(self):
        api = FakeApi({"now_playing": [1, 2], "upcoming": f.TmdbError("liste hatası")},
                      {1: f.TmdbError("x"), 2: detail(2)})
        res = self.run_it(api)
        self.assertEqual((res["new"], res["lists_ok"]), (1, 1))
        self.assertEqual(len(res["errors"]), 2)

    def test_empty_never_overwrites_good_rss(self):
        self.run_it(FakeApi({"now_playing": [1]}, {1: detail(1)}))
        before = open(os.path.join(self.root, "rss.xml"), encoding="utf-8").read()
        os.remove(os.path.join(self.root, "data", "content", "1.html"))                 # içerik kaybı
        res = self.run_it(FakeApi({"now_playing": []}, {}))
        self.assertEqual(res["in_feed"], 0)
        self.assertEqual(open(os.path.join(self.root, "rss.xml"), encoding="utf-8").read(), before)

    def test_old_content_pruned_but_record_kept(self):
        self.run_it(FakeApi({"now_playing": [1]}, {1: detail(1)}))
        later = datetime(2026, 12, 25, 6, 0, 0, tzinfo=timezone.utc)
        api = FakeApi({"now_playing": [1, 2]}, {1: detail(1), 2: detail(2)})
        self.run_it(api, now=later)
        self.assertFalse(os.path.exists(os.path.join(self.root, "data", "content", "1.html")))
        self.assertEqual(api.detail_calls, [2])                                          # 1 tekrar üretilmedi
        self.assertEqual(len(json.load(open(os.path.join(self.root, "data", "index.json")))["articles"]), 2)

    def test_count_limit_and_corrupt_index_backup(self):
        api = FakeApi({"now_playing": [1, 2, 3]}, {i: detail(i) for i in (1, 2, 3)})
        self.assertEqual(self.run_it(api, limit=2)["new"], 2)
        p = os.path.join(self.root, "data", "index.json")
        open(p, "w").write("{bozuk")
        res = self.run_it(api)                                                          # .bak'tan devam eder
        self.assertEqual(res["new"], 1)


class TestMain(Base):
    def setUp(self):
        super().setUp()
        self.saved = (f.TMDB_API_KEY, f.Api)

    def tearDown(self):
        f.TMDB_API_KEY, f.Api = self.saved
        super().tearDown()

    def test_exit_codes(self):
        f.TMDB_API_KEY = ""
        self.assertEqual(f.main(["--root", self.root]), 2)                               # anahtar yok
        f.TMDB_API_KEY = "k"
        f.Api = lambda key: FakeApi({"now_playing": [1], "upcoming": [2]}, {1: detail(1), 2: detail(2)})
        self.assertEqual(f.main(["--root", self.root]), 0)
        self.assertEqual(len(self.items()[1]), 2)
        f.Api = lambda key: FakeApi({"now_playing": f.TmdbError("x"), "upcoming": f.TmdbError("y")}, {})
        self.assertEqual(f.main(["--root", self.root]), 1)                               # TMDB erişilemez: iş kırmızı biter, eski RSS yayında kalır
        self.assertEqual(len(self.items()[1]), 2)                                        # mevcut rss.xml bozulmadı
        shutil.rmtree(os.path.join(self.root, "data"))
        os.remove(os.path.join(self.root, "rss.xml"))
        self.assertEqual(f.main(["--root", self.root]), 1)                               # liste yok, kayıt yok


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestClient(unittest.TestCase):
    def make(self, steps):
        seen = []

        def opener(req, timeout=0):
            seen.append(req)
            s = steps.pop(0)
            if isinstance(s, Exception):
                raise s
            return _Resp(json.dumps(s).encode())
        return opener, seen

    def err(self, code, retry=None):
        h = {"Retry-After": str(retry)} if retry else {}
        return urllib.error.HTTPError("u", code, "x", h, io.BytesIO(b"{}"))

    def test_retry_429_and_v3_key(self):
        sleeps = []
        opener, seen = self.make([self.err(429, 3), self.err(503), {"ok": 1}])
        api = f.Api("KEY123", opener=opener, sleep=sleeps.append)
        self.assertEqual(api.get("/x"), {"ok": 1})
        self.assertIn("api_key=KEY123", seen[0].full_url)
        self.assertEqual(sleeps[0], 3)

    def test_v4_token_uses_header(self):
        opener, seen = self.make([{"ok": 1}])
        f.Api("eyJabc.def.ghi", opener=opener, sleep=lambda s: None).get("/x")
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer eyJabc.def.ghi")
        self.assertNotIn("api_key", seen[0].full_url)

    def test_fatal_codes_and_exhausted_retries(self):
        opener, _ = self.make([self.err(401)])
        with self.assertRaises(f.TmdbError):
            f.Api("k", opener=opener, sleep=lambda s: None).get("/x")
        opener, seen = self.make([urllib.error.URLError("down")] * 5)
        with self.assertRaises(f.TmdbError):
            f.Api("k", opener=opener, sleep=lambda s: None, retries=4).get("/x")
        self.assertEqual(len(seen), 5)


if __name__ == "__main__":
    unittest.main()
