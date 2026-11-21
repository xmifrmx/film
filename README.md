# Film RSS (TMDB)

Türkiye'de vizyondaki ve yakında vizyona girecek filmler için günde iki kez RSS üretir
(afiş, fragman, film bilgileri, konu, oyuncular). Çıktı yalnızca `rss.xml`'dir ve GitHub Pages'e
doğrudan dağıtılır; depoya veri yazılmaz. Film verileri TMDB'den alınır
(bu ürün TMDB API'sini kullanır, ancak TMDB tarafından onaylanmamış veya sertifikalandırılmamıştır).

## Kurulum
1. TMDB hesabı açıp API anahtarı alın (themoviedb.org → Ayarlar → API).
2. Depoda **Settings → Secrets and variables → Actions → New repository secret**:
   Name `TMDB_API_KEY`, Secret = anahtarınız (v3 anahtarı ya da "API Read Access Token").
3. **Settings → Pages → Source: GitHub Actions**.
4. **Actions → "Film RSS (TMDB)" → Run workflow**.
5. RSS adresi: `https://KULLANICI.github.io/DEPO/rss.xml`
   (depo adı `KULLANICI.github.io` ise `https://KULLANICI.github.io/rss.xml`).

## Çalışma mantığı
* 06:17 ve 14:47 (TSİ) çalışır; aynı film (TMDB kimliği) bir daha üretilmez.
* Kayıt `data/index.json` dosyasında, Actions önbelleğinde tutulur.
* Konusu kısa / afişi olmayan filmler atlanır ve sonraki çalışmalarda yeniden denenir.
* Fragman veya oyuncu listesi sonradan gelen filmler 7 gün boyunca aynı kimlikle tamamlanır.

## Ayarlar (isteğe bağlı, workflow `env:` içine eklenir)
`FEED_ITEMS` (100), `MAX_PAGES` (3), `CAST_LIMIT` (12), `FILM_LISTS` (`now_playing,upcoming`),
`MIN_OVERVIEW_CHARS` (80), `CONTENT_KEEP_DAYS` (30).
