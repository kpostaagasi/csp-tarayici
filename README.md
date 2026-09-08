# csp-tarayici

Cash-secured put tarayıcı. Tek dosya, stdlib-only (`curses` dahil), API key yok.

```bash
python3 csp.py --tui NOK F MARA SOFI RIOT ETHA   # ilk sefer: izleme listesini kaydeder
python3 csp.py --tui                             # sonrası: kayıtlı listeyle açılır
python3 csp.py NOK SOFI --capital 2000           # tek seferlik tablo
python3 csp.py --selftest
```

TUI tuşları: `↑↓`/`jk` gezin · `enter` seçili ismin tüm geçen kontratları · `q` geri/çık ·
`s` sıralama (score→ROC→IV→cushion→spread→DTE) · `a`/`x` sembol ekle/çıkar ·
`c` sermaye · `d` delta bandı · `t` DTE aralığı · `e` kazanç filtresi · `r` yeniden tara.

## Veri

| Ne | Kaynak |
|---|---|
| Zincir: spot, `iv30`, bid/ask/IV/delta/OI | `cdn.cboe.com/api/global/delayed_quotes/options/{SYM}.json` |
| Günlük OHLC (gerçekleşen oynaklık) | `cdn.cboe.com/api/global/delayed_quotes/charts/historical/{SYM}.json` |
| Tahmini kazanç tarihi | `api.nasdaq.com/api/analyst/{SYM}/earnings-date` |

IV ve RV aynı vendor'dan geliyor; farkları apples-to-apples. CBOE Cloudflare arkasında
burst'te 429 veriyor: istekler global 0.35 sn'lik kapıdan geçiyor, `Retry-After` dinleniyor.
Kazanç tarihleri `~/.csp_earnings.json`'da 7 gün cache'leniyor, izleme listesi
`~/.csp_watchlist.json`'da tutuluyor.

## Skor

```
score = 100 × (0.35·vrp + 0.25·liq + 0.20·yield + 0.20·cushion)
```

- **vrp** — `log(IV30/RV30)/0.40`, kırpılmış. Varyans risk primi; tek gerçek alfa iddiası.
- **liq** — %60 bağıl spread `(ask−bid)/mid`, %40 açık pozisyon (log ölçek).
- **yield** — teminata göre yıllıklandırılmış prim; %8→0, %40→1.
- **cushion** — `(spot−strike)/(spot·EM)`, yani OTM mesafesi beklenen hareket birimiyle.
  `EM = IV30·√(DTE/365)` (takvim günü konvansiyonu).

Sert filtreler: DTE 21–45, |delta| 0.15–0.35, spread ≤ %10, OI ≥ 25, teminat ≤ `--capital`,
vade içinde kazanç varsa ele (`--allow-earnings` kapatır). Ağırlıklar ve eşikler dosyanın
başındaki sabitlerde; asıl ayar düğmesi orası.

Skor bir önsav, kanıt değil — geri-test yok. IV30 ileriye, RV30 geriye bakar; son ay sert
hareket olduysa `vrp` bileşeni bastırılır.

## Yapılmayanlar

Geri-test, temel/iflas riski (Merton distance-to-default), faktör yoğunlaşma cezası,
IV rank/percentile, lognormal beklenen hareket bandı, pozisyon takibi/günlük,
otomatik yenileme. Dar terminalde (<130 sütun) skor kolonu kesilir.
