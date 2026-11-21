#!/usr/bin/env python3
"""Film RSS üretici (TMDB API): Türkiye'de vizyondaki ve yakında vizyona girecek filmler.

Her film için RSS'e tam makale girer: afiş, fragman (YouTube), film bilgileri, konu ve oyuncular.
Puan, buton, yorum gibi alanlar hiç alınmaz.

* Yalnızca rss.xml üretilir (sitemap / feed / makale sayfası yok).
* Aynı film (TMDB kimliği) bir daha asla üretilmez; kayıt data/index.json içindedir (Actions önbelleği).
* Konusu kısa/afişi yok filmler atlanır ve sonraki çalışmalarda (en fazla MAX_SKIP_ATTEMPTS kez) yeniden denenir.
* Fragmanı ya da oyuncu listesi henüz yoksa makale yine yayınlanır, RECHECK_DAYS gün boyunca aynı kimlikle tamamlanır.

Gerekli ortam değişkeni: TMDB_API_KEY (v3 anahtarı ya da v4 "API Read Access Token").
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape, quoteattr

# ----------------------------------------------------------------------------- ayarlar
def _int(name, default):
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


SITE_URL = (os.environ.get("SITE_URL") or "https://xmifrmx.github.io/makale").rstrip("/")
SITE_ROOT = os.environ.get("SITE_ROOT", ".")
SITE_TITLE = "Sinema Rehberi – Vizyondaki ve Yakında Gelen Filmler"
SITE_DESC = ("Türkiye'de vizyondaki ve yakında vizyona girecek filmler: fragman, konu ve oyuncular. "
             "Film verileri TMDB'den alınır.")
CONTACT = os.environ.get("WIKI_CONTACT") or "https://github.com/xmifrmx/makale"
USER_AGENT = f"FilmRSS/1.0 ({CONTACT}; GitHub Actions)"
TMDB_API_KEY = (os.environ.get("TMDB_API_KEY") or "").strip()
LANG = "tr-TR"
REGION = "TR"
LISTS = tuple(x.strip() for x in (os.environ.get("FILM_LISTS") or "now_playing,upcoming").split(",") if x.strip())
LIST_LABEL = {"now_playing": "Vizyondakiler", "upcoming": "Yakında Vizyonda"}
MAX_PAGES = _int("MAX_PAGES", 3)                    # her liste için en fazla sayfa (sayfa başına 20 film)
FILMS_PER_RUN = _int("FILMS_PER_RUN", 0)            # bir çalışmada en fazla yeni film (0 = sınırsız)
FEED_ITEMS = _int("FEED_ITEMS", 100)                # RSS'teki en yeni film sayısı
CAST_LIMIT = _int("CAST_LIMIT", 12)
MIN_OVERVIEW_CHARS = _int("MIN_OVERVIEW_CHARS", 80)  # bundan kısa konu = makale yok (sonra yeniden denenir)
MAX_SKIP_ATTEMPTS = _int("MAX_SKIP_ATTEMPTS", 6)
RECHECK_DAYS = _int("RECHECK_DAYS", 7)               # eksik (fragman/oyuncu) makaleler için geriye bakış
CONTENT_KEEP_DAYS = _int("CONTENT_KEEP_DAYS", 30)    # makale HTML dosyaları bu kadar gün saklanır
VIDEO_MODE = (os.environ.get("VIDEO_MODE") or "iframe").strip().lower()   # iframe | thumb (iframe'i silen okuyucular için)
BUILD_VERSION = 1
IMG = "https://image.tmdb.org/t/p"

COUNTRIES = {
    "Turkey": "Türkiye", "United States of America": "ABD", "United Kingdom": "Birleşik Krallık",
    "France": "Fransa", "Germany": "Almanya", "Italy": "İtalya", "Spain": "İspanya", "Japan": "Japonya",
    "South Korea": "Güney Kore", "China": "Çin", "India": "Hindistan", "Canada": "Kanada", "Russia": "Rusya",
    "Australia": "Avustralya", "Brazil": "Brezilya", "Mexico": "Meksika", "Sweden": "İsveç",
    "Denmark": "Danimarka", "Netherlands": "Hollanda", "Belgium": "Belçika", "Poland": "Polonya",
    "Ireland": "İrlanda", "Greece": "Yunanistan", "Iran": "İran", "Argentina": "Arjantin", "Norway": "Norveç",
    "Finland": "Finlandiya", "Switzerland": "İsviçre", "Austria": "Avusturya", "Czech Republic": "Çekya",
    "New Zealand": "Yeni Zelanda", "Egypt": "Mısır", "Israel": "İsrail", "Thailand": "Tayland",
    "Ukraine": "Ukrayna", "Romania": "Romanya", "Hungary": "Macaristan", "Portugal": "Portekiz",
    "Indonesia": "Endonezya", "Philippines": "Filipinler", "Saudi Arabia": "Suudi Arabistan",
    "United Arab Emirates": "Birleşik Arap Emirlikleri", "South Africa": "Güney Afrika", "Taiwan": "Tayvan",
}
AYLAR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]

esc = html.escape
_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def xml_clean(s):
    return _BAD_XML.sub("", s or "")


# ----------------------------------------------------------------------------- TMDB istemcisi
class TmdbError(RuntimeError):
    pass


def _retry_after(err):
    try:
        return int(err.headers.get("Retry-After"))
    except (TypeError, ValueError, AttributeError):
        return None


class Api:
    BASE = "https://api.themoviedb.org/3"

    def __init__(self, key, opener=None, sleep=time.sleep, retries=4, pause=0.06):
        self.key, self.sleep, self.retries, self.pause = (key or "").strip(), sleep, retries, pause
        self.open = opener or urllib.request.urlopen

    def get(self, path, **params):
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if self.key.startswith("eyJ"):                       # v4 okuma belirteci (JWT)
            headers["Authorization"] = "Bearer " + self.key
        else:                                                # v3 API anahtarı
            params["api_key"] = self.key
        url = f"{self.BASE}{path}?{urllib.parse.urlencode(params)}"
        last = None
        for n in range(self.retries + 1):
            wait = 2 ** n
            try:
                with self.open(urllib.request.Request(url, headers=headers), timeout=25) as r:
                    data = json.loads(r.read().decode("utf-8"))
                self.sleep(self.pause)
                return data
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise TmdbError(f"TMDB yetkisiz ({e.code}): TMDB_API_KEY geçersiz olabilir")
                if e.code == 404:
                    raise TmdbError(f"TMDB 404: {path}")
                if e.code not in (429, 500, 502, 503, 504):
                    raise TmdbError(f"TMDB HTTP {e.code}: {path}")
                last, wait = f"HTTP {e.code}", _retry_after(e) or wait
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                last = str(e)
            if n < self.retries:
                self.sleep(min(wait, 30))
        raise TmdbError(f"TMDB yanıt vermedi ({last}): {path}")

    def movie_list(self, kind, page=1):
        return self.get(f"/movie/{kind}", language=LANG, region=REGION, page=page)

    def movie_detail(self, mid):
        return self.get(f"/movie/{mid}", language=LANG, append_to_response="credits,videos,release_dates",
                        include_video_language="tr,en,null")


# ----------------------------------------------------------------------------- biçimlendirme
def shorten(text, limit=155):
    t = " ".join((text or "").split())
    if len(t) <= limit:
        return t
    return t[:limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-–—") + "…"


def tr_date(iso):
    try:
        y, m, d = (int(x) for x in (iso or "")[:10].split("-"))
        return f"{d} {AYLAR[m - 1]} {y}"
    except (ValueError, IndexError):
        return ""


def tr_runtime(minutes):
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return ""
    if m <= 0:
        return ""
    h, mm = divmod(m, 60)
    return " ".join(p for p in ((f"{h} saat" if h else ""), (f"{mm} dakika" if mm else "")) if p)


def release_date(d):
    """Türkiye sinema vizyon tarihi (varsa), yoksa TMDB'nin genel çıkış tarihi."""
    found = []
    for c in (d.get("release_dates") or {}).get("results", []):
        if c.get("iso_3166_1") == REGION:
            found += [r["release_date"][:10] for r in c.get("release_dates", [])
                      if r.get("type") in (2, 3) and r.get("release_date")]
    return min(found) if found else (d.get("release_date") or "")


def pick_trailer(d):
    vids = [v for v in (d.get("videos") or {}).get("results", [])
            if v.get("site") == "YouTube" and v.get("type") in ("Trailer", "Teaser")
            and re.fullmatch(r"[\w-]{6,20}", v.get("key") or "")]
    vids.sort(key=lambda v: v.get("published_at") or "", reverse=True)
    vids.sort(key=lambda v: (v.get("iso_639_1") != "tr", v.get("type") != "Trailer", not v.get("official")))
    return vids[0] if vids else None


def _people(d, jobs, limit):
    names = []
    for job in jobs:
        for p in (d.get("credits") or {}).get("crew", []):
            n = (p.get("name") or "").strip()
            if p.get("job") == job and n and n not in names:
                names.append(n)
    return names[:limit]


# ----------------------------------------------------------------------------- makale
def build_article(d, kind, now, keep=None):
    """TMDB film ayrıntısından (rec, body) üretir. Uygun değilse (None, neden) döner."""
    mid = d.get("id")
    title = " ".join((d.get("title") or "").split())
    if not mid or not title:
        return None, "bos_ad"
    raw = (d.get("overview") or "").replace("\r", "")
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", raw) if p.strip()]
    overview = " ".join(paras)
    if len(overview) < MIN_OVERVIEW_CHARS:
        return None, "konu_kisa"
    poster = d.get("poster_path")
    if not poster:
        return None, "afis_yok"

    t, gaps = esc(title), []
    link = f"https://www.themoviedb.org/movie/{mid}"
    out = [f'<div class="separator" style="clear: both; text-align: center; margin-bottom: 1em;">'
           f'<a href="{IMG}/original{poster}" rel="noopener" target="_blank">'
           f'<img alt="{t} afişi" border="0" height="750" src="{IMG}/w500{poster}" '
           f'style="max-width: 100%; height: auto;" width="500" /></a></div>']

    tag = " ".join((d.get("tagline") or "").split())
    if tag:
        out.append(f"<p><i>{esc(tag, quote=False)}</i></p>")

    # --- fragman (Konusu bölümünün içine gömülür)
    video = ""
    tr = pick_trailer(d)
    if tr:
        k = tr["key"]
        watch = f"https://www.youtube.com/watch?v={k}"
        if VIDEO_MODE == "thumb":        # iframe'i silen okuyucular için: tıklanabilir önizleme görseli
            video = (f'<p style="text-align: center;"><a href="{watch}" rel="noopener" target="_blank">'
                     f'<img alt="{t} fragmanını izle" height="360" src="https://img.youtube.com/vi/{k}/hqdefault.jpg" '
                     f'style="max-width: 100%; height: auto;" width="480" /></a></p>')
        else:
            video = (f'<div class="separator" style="clear: both; text-align: center;">'
                     f'<iframe allowfullscreen="allowfullscreen" frameborder="0" height="394" loading="lazy" '
                     f'src="https://www.youtube.com/embed/{k}" title="{t} fragmanı" width="700"></iframe></div>')
        video += (f'<p style="text-align: center;"><a href="{watch}" rel="noopener" target="_blank">'
                  f'{t} fragmanını YouTube\'da izle</a></p>')
    else:
        gaps.append("fragman")

    # --- film bilgileri
    year = (d.get("release_date") or "")[:4]
    countries = [COUNTRIES.get(c.get("name"), c.get("name")) for c in d.get("production_countries") or [] if c.get("name")]
    genres = [g["name"] for g in d.get("genres") or [] if g.get("name")]
    facts = " • ".join(x for x in (year, tr_runtime(d.get("runtime")), ", ".join(countries[:2])) if x)
    rows = [("Tür", ", ".join(genres)), ("Künye", facts), ("Vizyon Tarihi", tr_date(release_date(d)))]
    orig = " ".join((d.get("original_title") or "").split())
    if orig and orig != title:
        rows.append(("Diğer Adı", orig))
    rows += [("Yönetmen", ", ".join(_people(d, ("Director",), 3))),
             ("Senarist", ", ".join(_people(d, ("Screenplay", "Writer"), 3)))]
    out.append(f"<h2>{t} Film Bilgileri</h2>")
    out.append("<p>" + "<br />".join(f"<b>{lbl}:</b> {esc(val, quote=False)}" for lbl, val in rows if val) + "</p>")

    # --- konu
    out.append(f"<h2>{t} Konusu</h2>")
    out += [f"<p>{esc(p, quote=False)}</p>" for p in paras]
    if video:
        out.append(video)

    # --- oyuncular
    cast = sorted((d.get("credits") or {}).get("cast", []), key=lambda c: c.get("order", 999))
    cast = [c for c in cast if (c.get("name") or "").strip()][:CAST_LIMIT]
    out.append(f"<h2>{t} Oyuncuları</h2>")
    if cast:
        cards = []
        for c in cast:
            name, role = esc(c["name"].strip()), esc((c.get("character") or "").strip())
            pic = (f'<img alt="{name}" height="180" src="{IMG}/w185{c["profile_path"]}" width="120" '
                   f'style="border-radius: 6px; max-width: 100%; height: auto;" />' if c.get("profile_path") else
                   '<span style="display: block; width: 120px; height: 180px; margin: 0 auto; '
                   'background: #e6e6e6; border-radius: 6px;"></span>')
            cards.append('<div style="display: inline-block; width: 120px; margin: 0 8px 14px 0; '
                         f'vertical-align: top; text-align: center;">{pic}<br /><b>{name}</b>'
                         + (f'<br /><span style="color: #666;">{role}</span>' if role else "") + "</div>")
        out.append("<div>" + "".join(cards) + "</div>")
    else:
        gaps.append("oyuncu")
        out.append("<p>Oyuncu bilgisi henüz yayınlanmadı.</p>")

    out.append(f'<p><small>Film verileri <a href="{link}" rel="noopener" target="_blank">TMDB</a> kaynaklıdır. '
               'Bu içerik TMDB API\'sini kullanır, ancak TMDB tarafından onaylanmamış veya sertifikalandırılmamıştır.'
               '</small></p>')
    body = "\n".join(out) + "\n"

    stamp = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    title_out = f"{title} Filmi: Fragman, Konu ve Oyuncular"
    rec = {
        "id": f"film-{mid}", "tmdb_id": mid, "kind": kind, "v": BUILD_VERSION,
        "pub": stamp(now), "date": now.strftime("%Y-%m-%d"),
        "title": title_out if len(title_out) <= 110 else title,
        "link": link, "description": shorten(overview), "gaps": gaps,
        "categories": ["Film", LIST_LABEL.get(kind, "Film")] + genres,
        "image": {"url": f"{IMG}/w500{poster}", "width": 500, "height": 750},
        "content_file": f"data/content/{mid}.html",
    }
    if keep:                                         # eksik tamamlamada kimlik ve yayın zamanı değişmez
        rec.update(id=keep["id"], pub=keep["pub"], date=keep["date"])
        rec["updated"] = stamp(now)
    return rec, body


# ----------------------------------------------------------------------------- RSS
def _dt(iso):
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def rfc822(dt):
    return format_datetime(dt.astimezone(timezone.utc), usegmt=False).replace("-0000", "+0000")


def cdata(s):
    return "<![CDATA[" + xml_clean(s).replace("]]>", "]]]]><![CDATA[>") + "]]>"


def build_rss(records, contents, now):
    items = []
    for r in records:
        body = contents.get(r["id"])
        if not body:
            continue
        img = r["image"]
        cats = "".join(f"      <category>{escape(xml_clean(c))}</category>\n" for c in r["categories"])
        items.append(
            "    <item>\n"
            f"      <title>{escape(xml_clean(r['title']))}</title>\n"
            f"      <link>{escape(r['link'])}</link>\n"
            f"      <guid isPermaLink=\"false\">{escape(r['id'])}</guid>\n"
            f"      <pubDate>{rfc822(_dt(r['pub']))}</pubDate>\n"
            "      <dc:creator>Sinema Rehberi</dc:creator>\n"
            f"{cats}"
            f"      <description>{escape(xml_clean(r['description']))}</description>\n"
            f"      <media:content url={quoteattr(img['url'])} medium=\"image\" width=\"{img['width']}\" height=\"{img['height']}\" />\n"
            f"      <content:encoded>{cdata(body)}</content:encoded>\n"
            "    </item>\n")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:atom="http://www.w3.org/2005/Atom" '
        'xmlns:media="http://search.yahoo.com/mrss/">\n'
        "  <channel>\n"
        f"    <title>{escape(SITE_TITLE)}</title>\n"
        f"    <link>{escape(SITE_URL)}/</link>\n"
        f"    <description>{escape(SITE_DESC)}</description>\n"
        "    <language>tr</language>\n"
        f"    <lastBuildDate>{rfc822(now)}</lastBuildDate>\n"
        "    <ttl>720</ttl>\n"
        "    <generator>filmrss (GitHub Actions, TMDB)</generator>\n"
        f'    <atom:link href="{escape(SITE_URL)}/rss.xml" rel="self" type="application/rss+xml" />\n'
        + "".join(items) + "  </channel>\n</rss>\n")


def write_text(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def write_feed(root, articles, now, log=print):
    """Yalnızca rss.xml yazar. Hiç öğe yoksa mevcut rss.xml'in üzerine yazılmaz (boş RSS yayınlanmasın)."""
    ordered = sorted(articles, key=lambda a: (a["pub"], a["id"]), reverse=True)
    latest = ordered[:FEED_ITEMS] if FEED_ITEMS > 0 else ordered
    contents = {}
    for r in latest:
        try:
            with open(os.path.join(root, r["content_file"]), encoding="utf-8") as f:
                contents[r["id"]] = f.read()
        except OSError:
            log(f"::warning::içerik dosyası eksik, RSS'e eklenmedi: {r['content_file']}")
    if not contents:
        log("::warning::RSS'e eklenecek içerik yok; rss.xml yazılmadı")
        return 0
    write_text(os.path.join(root, "rss.xml"), build_rss(latest, contents, now))
    return len(contents)


# ----------------------------------------------------------------------------- kalıcı kayıt
class StoreError(RuntimeError):
    pass


def _index_path(root):
    return os.path.join(root, "data", "index.json")


def _read_index(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not (isinstance(data, dict) and isinstance(data.get("articles"), list)):
        raise ValueError("beklenmeyen biçim")
    return data


def store_load(root, log=print):
    p = _index_path(root)
    for cand in (p, p + ".bak"):
        if os.path.exists(cand):
            try:
                data = _read_index(cand)
                if cand != p:
                    log("::warning::index.json okunamadı, .bak yedeğinden devam ediliyor")
                if not isinstance(data.get("skips"), dict):
                    data["skips"] = {}
                return data
            except (OSError, ValueError):
                continue
    if os.path.exists(p) or os.path.exists(p + ".bak"):
        raise StoreError("data/index.json bozuk ve yedek okunamadı")
    return {"version": 1, "articles": [], "skips": {}}


def store_save(root, data, backup=True):
    p = _index_path(root)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if backup and os.path.exists(p):
        try:
            _read_index(p)
            shutil.copyfile(p, p + ".bak")
        except (OSError, ValueError):
            pass
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


# ----------------------------------------------------------------------------- akış
def collect(api, log):
    """Vizyondaki + yakında vizyona girecek film kimlikleri (tekrarsız). -> (liste, başarılı_liste_sayısı, hatalar)"""
    out, seen, ok, errors = [], set(), 0, []
    for kind in LISTS:
        try:
            page, total = 1, 1
            while page <= min(total, MAX_PAGES):
                j = api.movie_list(kind, page)
                total = int(j.get("total_pages") or 1)
                for m in j.get("results", []):
                    mid = m.get("id")
                    if mid and not m.get("adult") and mid not in seen:
                        seen.add(mid)
                        out.append((mid, kind))
                page += 1
            ok += 1
        except TmdbError as e:
            log(f"::warning::{kind} listesi alınamadı: {e}")
            errors.append(str(e))
    return out, ok, errors


def run(root=None, api=None, now=None, limit=None, log=print):
    root = root or SITE_ROOT
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    api = api or Api(TMDB_API_KEY)
    data = store_load(root, log)
    arts, skips = data["articles"], data["skips"]
    known = {a["tmdb_id"] for a in arts}

    cands, lists_ok, errors = collect(api, log)
    todo = [(m, k) for m, k in cands if m not in known and skips.get(str(m), {}).get("n", 0) < MAX_SKIP_ATTEMPTS]
    cap = FILMS_PER_RUN if limit is None else limit
    if cap and cap > 0:
        todo = todo[:cap]
    log(f"aday: {len(cands)} | zaten yayımlanmış: {sum(1 for m, _ in cands if m in known)} | işlenecek: {len(todo)}")

    new, streak, first = [], 0, True
    for mid, kind in todo:
        try:
            d = api.movie_detail(mid)
        except TmdbError as e:
            log(f"::warning::film alınamadı ({mid}): {e}")
            errors.append(str(e))
            streak += 1
            if streak >= 5:
                log("::warning::ardışık API hatası: üretim durduruldu, kalanlar sonraki çalışmada işlenecek")
                break
            continue
        streak = 0
        rec, extra = build_article(d, kind, now + timedelta(seconds=len(new)))
        if rec is None:
            skips[str(mid)] = {"n": skips.get(str(mid), {}).get("n", 0) + 1, "r": extra}
            log(f"- atlandı ({extra}): {d.get('title') or mid}")
            continue
        write_text(os.path.join(root, rec["content_file"]), extra)
        arts.append(rec)
        new.append(rec)
        skips.pop(str(mid), None)
        store_save(root, data, backup=first)             # her makaleden sonra kalıcılaştır
        first = False
        log(f"+ {rec['title']}" + (f"  eksik={rec['gaps']}" if rec["gaps"] else ""))

    # fragman / oyuncu sonradan gelen filmleri aynı kimlikle tamamla
    floor = (now - timedelta(days=RECHECK_DAYS)).strftime("%Y-%m-%d")
    fresh, updated = {r["id"] for r in new}, 0
    for r in arts:
        if not r.get("gaps") or r["date"] < floor or r["id"] in fresh:
            continue
        try:
            d = api.movie_detail(r["tmdb_id"])
        except TmdbError as e:
            log(f"::warning::tamamlama için film alınamadı ({r['tmdb_id']}): {e}")
            continue
        rec, extra = build_article(d, r.get("kind", "now_playing"), now, keep=r)
        if rec is None or len(rec["gaps"]) >= len(r["gaps"]):
            continue
        write_text(os.path.join(root, rec["content_file"]), extra)
        r.clear()
        r.update(rec)
        updated += 1
        log(f"~ tamamlandı: {rec['title']}  (kalan eksik: {rec['gaps'] or 'yok'})")

    # eski içerik dosyalarını temizle (kayıt kalır: aynı film tekrar üretilmez)
    old = (now - timedelta(days=CONTENT_KEEP_DAYS)).strftime("%Y-%m-%d")
    for r in arts:
        if r["date"] < old:
            try:
                os.remove(os.path.join(root, r["content_file"]))
            except OSError:
                pass

    in_feed = write_feed(root, arts, now, log)
    store_save(root, data)
    log(f"ÖZET yeni={len(new)} tamamlanan={updated} rss_öğe={in_feed} toplam_kayıt={len(arts)} "
        f"bekleyen_atlanan={sum(1 for v in skips.values() if v.get('n', 0) < MAX_SKIP_ATTEMPTS)}")
    return {"new": len(new), "updated": updated, "in_feed": in_feed, "errors": errors, "lists_ok": lists_ok}


def main(argv=None):
    ap = argparse.ArgumentParser(description="TMDB tabanlı film RSS üretici")
    ap.add_argument("--root", default=SITE_ROOT)
    ap.add_argument("--count", type=int, help="bir çalışmada en fazla yeni film (boş/0 = sınırsız)")
    a = ap.parse_args(argv)
    if not TMDB_API_KEY:
        print("::error::TMDB_API_KEY tanımlı değil. Settings → Secrets and variables → Actions → New repository secret")
        return 2
    res = run(root=a.root, limit=a.count)
    if res["lists_ok"] == 0:
        print("::error::Film listeleri alınamadı: " + "; ".join(res["errors"] or ["bilinmeyen hata"]))
        return 1
    if res["in_feed"] == 0:
        print("::error::RSS'e eklenecek film yok: " + "; ".join(res["errors"] or ["uygun film bulunamadı"]))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
