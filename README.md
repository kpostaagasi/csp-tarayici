# csp-tarayici

Cash-secured put tarayıcı: CBOE zinciri + gerçekleşen oynaklık + kazanç takvimi,
stdlib-only çalışma zamanı (`curses` dahil), **API key yok**. Terminal TUI'si, düz Türkçe
açıklama paneli ve kendi kaydettiği zincirlerle çalışan gerçek bir backtest motoru var.

> Cash-secured put scanner for small accounts: CBOE delayed chains, variance-risk-premium
> scoring, curses TUI, self-recorded option-chain history for real backtests. Python
> standard library only at runtime, no API keys. Turkish UI.

## Kurulum

```bash
brew trust kpostaagasi/csp                    # Homebrew 6+ üçüncü taraf tap'ler için şart
brew install kpostaagasi/csp/csp-tarayici
```

Ya da PyPI'dan:

```bash
uv tool install csp-tarayici
# veya
pipx install csp-tarayici
```

Ya da kaynaktan:

```bash
git clone https://github.com/kpostaagasi/csp-tarayici && cd csp-tarayici
make install      # pip install -e ".[dev]"
make check        # pytest + ruff check + ruff format --check — CI'ın koştuğu tek kapı
```

Python 3.10+ (`curses`, `sqlite3` stdlib'de). Kurulumdan sonra komut adı `csp`.

```bash
csp --universe --tui                  # sermayeyle alınabilen en işlek 60 ABD hissesi
csp --universe 150 --capital 5000     # evreni büyüt (ilk tarama ~N/0.4 saniye)
csp --tui NOK F MARA SOFI RIOT ETHA   # elle liste: ilk sefer kaydedilir
csp --tui                             # sonrası: kayıtlı listeyle açılır
csp NOK SOFI --capital 2000           # tek seferlik tablo
csp NOK SOFI --explain                # her satırın altına düz Türkçe okuma
csp --universe --min-iv-rank 0.5      # vol'ü kendi kaydettiğin aralığın üst yarısında olanlar
csp --universe --snapshot             # bugünün put zincirlerini diske yaz (günlük cron)
csp --replay NOK --capital 2000       # kayıtlı zincirlerle gerçek backtest
csp --replay TÜMÜ --capital 5000      # kaydettiğin her sembol, tek portföy olarak
csp --replay TÜMÜ --take-profit 0.5   # primin yarısı kalınca geri al: vadeye taşımaya karşı ölç
```

TUI tuşları: `?` **yardım — her kolonun ne demek olduğu** · `↑↓`/`jk` gezin ·
`enter` seçili ismin tüm geçen kontratları · `q` geri/çık ·
`s` sıralama · `a`/`x` sembol ekle/çıkar ·
`c` sermaye · `d` delta bandı · `t` DTE aralığı · `e` kazanç filtresi · `r` veriyi yenile.
`s` sırası: score → **EV$ (geçmiş testi)** → **IVR (IV rank)** → ROC → IV → cushion → spread → DTE.

`r` dışındaki her düğme cache'lenmiş zincirleri yeniden filtreliyor: ağ yok, sonuç anında
(5 sembolde 2.0 sn → 0.01 sn). `r` zincirleri sıfırlayıp yeniden çekiyor. `--universe`
modunda `a`/`x` kayıtlı izleme listesini ezmiyor.

Ekranın alt dördü seçili kontratın düz okuması: prim, bloke nakit, atama olasılığı, başabaş
fiyat, piyasanın vadeye kadar beklediği hareket ve skorun hangi bileşenden geldiği. Aynı metin
CLI'da `--explain` ile geliyor. Kolon seti terminal genişliğine göre kırpılıyor: önce `yld/liq/vrp`,
sonra `spot/exp/OI/RV30/sprd%` düşer; `sym strike dte ROC%y cush score` asla düşmez.

## Yapı

Paket artık tek dosya değil, `csp/` altında modüllere ayrıldı:

| Modül | Sorumluluk |
|---|---|
| `csp/http.py` | Rate-gate'li HTTP istekleri |
| `csp/cache.py` | Disk cache (kilitli yazma) |
| `csp/sources.py` | CBOE opsiyon zinciri + günlük barlar, Nasdaq kazanç tarihi + screener |
| `csp/score.py` | `Filters`/`Candidate`, skor bileşenleri, paralel tarama |
| `csp/universe.py` | Sermayeye göre evren seçimi |
| `csp/backtest.py` | EV$ (geçmiş testi), zincir kaydı (`--snapshot`), `--replay` |
| `csp/render.py` | Genişliğe uyan kolonlar, açıklama paneli |
| `csp/tui.py` | curses arayüz |
| `csp/cli.py` | argparse giriş noktası (`csp` komutu) |

Filtreler artık dosyanın başında global sabitler değil, `Filters` dataclass'ı olarak taşınıyor.

## Neden bağımlılık yok

Darboğaz CBOE'nin hız sınırı — bir HTTP kütüphanesi (requests/httpx) burada zaman
kazandırmıyor, kazandıran şey rate gate. Hesap tarafındaki iş milisaniye altı; numpy/pandas
gerekçesi yok. `curses` zaten full-screen TUI veriyor: Textual/Rich gibi bir kütüphane 15 MB'lık
bağımlılığı ~40 satırlık kaydırma/kolon kırpma koduyla takas ederdi — kötü bir takas.
Geliştirme tarafında `pytest` ve `ruff` var — ikisi de çalışma zamanına girmiyor.

## Evren

`--universe N`: Nasdaq screener'dan tüm ABD listeli hisseler (~7100 satır, tek istek) çekilip
`fiyat ≤ sermaye/100`, `hacim ≥ 1M`, `piyasa değeri ≥ $300M`, sembol `^[A-Z]{1,5}$` süzgecinden
geçiriliyor; hacme göre en işlek N tanesi taranıyor. $2000 sermayede bu ~1500 aday demek, o yüzden
kesim ağdan **önce** yapılıyor. Liste `~/.csp_universe.json`'da gün boyu duruyor.

Maliyet dürüstçe: kapı istek başlangıçları arasında 0.35 sn bırakıyor, yani ~2.9 istek/sn.
60 sembollük ilk tarama en fazla ~180 istek (zincir + günlük barlar + kazanç tarihi) = kağıt
üzerinde ~1 dakika; aynı gün içindeki sonraki taramalar barları ve kazanç tarihlerini cache'ten
okuduğu için ~20 saniyeye iniyor. Ölçülen (2026-09-08): 12 zincir isteği, sıfır 429. Sürekli
yükte 429 çıkarsa `Retry-After` + `2**i` geri çekilmesi süreyi birkaç katına çıkarabilir — bunu
kimse 180 istekte ölçmedi, o yüzden 0.35 sn olduğu yerde duruyor. Sonraki filtre değişiklikleri
bedava (zincir cache'i), yalnız `r` yeniden ağa çıkıyor.

## Veri

| Ne | Kaynak |
|---|---|
| Zincir: spot, `iv30`, bid/ask/IV/delta/OI | `cdn.cboe.com/api/global/delayed_quotes/options/{SYM}.json` |
| Günlük OHLC (gerçekleşen oynaklık) | `cdn.cboe.com/api/global/delayed_quotes/charts/historical/{SYM}.json` |
| Tahmini kazanç tarihi | `api.nasdaq.com/api/analyst/{SYM}/earnings-date` |
| Tüm ABD listeli hisseler (fiyat/hacim/piyasa değeri) | `api.nasdaq.com/api/screener/stocks?download=true` |

IV ve RV aynı vendor'dan geliyor; farkları apples-to-apples. Zincirler süreç içinde 10 dakika
(`CHAIN_TTL`), kazanç tarihleri `~/.csp_earnings.json`'da 7 gün, günlük kapanışlar
`~/.csp_hist.json`'da ve evren listesi `~/.csp_universe.json`'da takvim günü boyunca tutuluyor.
İzleme listesi `~/.csp_watchlist.json`. Paralel tarama cache dosyalarını kilitle yazıyor;
yarım yazılmış bir cache traceback değil boş cache sayılıyor.

## Skor

```
score = 100 × (0.35·vrp + 0.25·liq + 0.20·yield + 0.20·cushion)
```

- **vrp** — `(log(IV/RV30) + 0.20) / 0.60`, kırpılmış. Varyans risk primi; tek gerçek alfa iddiası.
  `IV` = satılan kontratın kendi implied vol'ü (ATM `iv30` değil): skew zaten sattığın şey.
  IV = RV → 0.33, IV/RV = 1.49 → 1.0, IV RV'nin %18 altına inince 0. Yani "IV < RV" artık
  tek bir 0 kümesinde toplanmıyor, sıralanıyor.
- **liq** — %60 bağıl spread `(ask−bid)/mid`, %40 açık pozisyon (log ölçek).
- **yield** — teminata göre yıllıklandırılmış prim; %8→0, %40→1.
- **cushion** — `(spot−strike)/(spot·EM)`, yani OTM mesafesi beklenen hareket birimiyle.
  `EM = IV·√(DTE/365)` (takvim günü konvansiyonu, kontratın kendi IV'si — strike'ın kendi
  dağılımı bu, ATM'in değil).

Sert filtreler: DTE 21–45, |delta| 0.15–0.35, spread ≤ %10, OI ≥ 25, teminat ≤ `--capital`,
vade içinde kazanç varsa ele (`--allow-earnings` kapatır), isteğe bağlı `--min-iv-rank`.
Ağırlıklar ve eşikler `Filters` dataclass'ının başındaki sabitlerde; asıl ayar düğmesi orası.

### IV rank (`IVR` kolonu) — skorun içinde değil, yanında

`vrp` "IV, gerçekleşenin üstünde mi" diye sorar; IV rank "bu IV, bu sembolün kendi geçmişine
göre nerede" diye sorar. Farklı bilgi: RV sakinken IV de sakinse `vrp` yüksek çıkabilir ama vol
kendi aralığının dibindedir.

Ücretsiz hiçbir yerde bu isimlerin IV geçmişi yok — ama `--snapshot` zaten her günün IV'sini
kaydediyor. `IVR`, kayıtlı her gün için 21–45 DTE ve |delta| 0.10–0.50 bandındaki putların
**medyan IV'si**ni alıp bugünkünü son 252 kayıtlı günün min–max aralığına yerleştiriyor.
Bantlar `Filters`'a değil sabitlere bağlı: DTE düğmesini genişletmek her sembolü yeniden
sıralasaydı, skor sabitlerinin kaçındığı tuzağa düşerdi.

**Skora girmiyor, bilerek.** Girdisi "bu makinenin ne kadar süredir kaydettiği" — bir yıldır
takip ettiğin sembolle dün eklediğin sembol farklı kanıtla puanlanırdı, oysa skorun bütün işi
onları tek tabloda karşılaştırmak. O yüzden kolon, panel satırı, sıralama anahtarı ve isteğe
bağlı filtre; ağırlık değil. 20 kayıtlı günden azsa `—` diyor, uydurmuyor.

`--replay` sırasında rank o günün kendi bilgisiyle hesaplanıyor: kontratın girildiği güne kadar
(o gün dahil, sonrası hariç) kaydedilenler — `realized_vol(before=...)` ile aynı kural.

Skor bir önsav, kanıt değil. IV ileriye, RV30 geriye bakar; son ay sert hareket olduysa `vrp`
bileşeni bastırılır. Skorun ne dediğini `EV$` ile karşılaştır: yüksek skor + eksi EV$, "primi
iyi ama bu hissenin kuyruğu o primi tarihsel olarak ödememiş" demek.

## Geçmiş testi (EV$)

Ücretsiz hiçbir kaynakta geçmiş opsiyon fiyatı yok, o yüzden burada opsiyon fiyatlanmıyor:
**prim bugünün gerçek mid'i, yalnız hissenin yolu tarihsel.** Kontratın `strike/spot` mesafesi
alınıp, sembolün tüm günlük kapanış geçmişindeki her `DTE` pencereye uygulanıyor:

```
adım   = round(DTE × 252/365)            # DTE takvim günü, barlar seans
otm    = strike/spot − 1                 # eksi: strike spot'un ne kadar altında
P&L(i) = prim×100 + min(0, r(i) − otm) × spot × 100,   r(i) = px[i+adım]/px[i] − 1
```

Yani vade sonunda uzlaşma: strike'ın üstünde bitti → primi aldın; altında → `strike − S_T`
kadar zarar. Erken atama ve roll yok. Panelin 5. satırı tüm çıktıyı veriyor:

```
geçmiş 22.6 yıl / 5681 pencere: bu mesafe %17 atama (piyasa ~%23) · %14 zarar
                                · ortalama +$11 · en kötü -$316
```

`EV$` kolonu bu "ortalama". CBOE geçmişi sembole göre 1.5–22.6 yıl; 120 pencereden az veri
varsa hiç konuşmuyor (`—` yerine "yeterli fiyat geçmişi yok"). Pencereler örtüşüyor, yani
örneklem otokorelasyonlu: bu bir üst-sınır tahmini, ispat değil. Rejim de sabit sayılıyor —
2008/2020'yi içeren geçmiş bugünün oynaklığıyla aynı değil.

## Gerçek backtest: `--snapshot` + `--replay`

`EV$` opsiyonun geçmiş fiyatını bilmiyor, hissenin yolunu biliyor. Gerçeğini yapmak için geçmiş
**zincir** verisi gerekiyor. Üç ücretsiz/ucuz aday ölçüldü:

| Kaynak | Kapsam | Ölçülen sorun |
|---|---|---|
| DoltHub `post-no-preference/options` | 2019→bugün, ~2300 sembol, bid/ask/IV/greeks, key yok | SQL endpoint sorgu başına **40-55 sn**; `2025-06-16`'da MARA var ama **SOFI/PLUG/NOK yok** — yani $2k hesabın satabileceği isimler eksik. Yerel `dolt clone` (8.5 GB) hızı çözer, kapsamı çözmez |
| OptionsDX | tüm opsiyonlu ABD isimleri, EOD zincir | yıl başına ~$50 tek seferlik, manuel CSV indirme |
| EODHD / Massive(Polygon) | 2.5 yıl / 10+ yıl | $30-79 **aylık**, API key |

Sonuç: küçük/orta sermaye evreni için bedava ve tam kapsamlı geçmiş zincir yok. Ama biz o
zincirleri skorlamak için **zaten indiriyoruz** — atmak yerine kaydetmek yeterli.

```bash
csp --universe --snapshot     # ~1200 satır/gün, 60 sembolde ~3 MB/ay
```

Bu defter aynı zamanda `IVR` kolonunun tek kaynağı: ne kadar uzun kaydedersen IV rank o kadar
anlamlı.

`~/.csp_chains.db` (SQLite): `puts(date, sym, exp, strike, bid, ask, iv, delta, oi, spot)`,
PK `(date, sym, exp, strike)`, DTE ≤ 70 olan putlar.

**Satırlar işin koştuğu takvim gününe değil, kotasyonların geldiği seansa damgalanıyor.** CBOE
tatilde ya da açılıştan önce bir önceki seansı servis ediyor; onu "bugün" diye kaydetmek IV rank
serisine uydurma bir gün, `--replay`'e uydurma bir giriş tarihi yazardı. Seansa damgalanınca aynı
kotasyonlar aynı birincil anahtara düşüyor ve tekrar çalıştırmak zararsız bir no-op oluyor:

```
$ csp NOK SOFI --snapshot
437 satır yazıldı · seans 2026-09-04  ⚠ bugün değil (piyasa kapalı / seans açılmadı)
defter: 437 satır / 1 gün / 2 sembol → ~/.csp_chains.db
```

### Günlük kayıt işi

Damga seanstan geldiği için **saati tutturmak zorunda değilsin**: geç kalmış ya da ertesi sabah
koşan bir iş de doğru seansı doğru tarihe yazar. En kolay tarifi bu: ABD kapanışını beklemek
yerine hafta içi her sabah koştur — Pazartesi sabahı Cuma'yı, Salı sabahı Pazartesi'yi alır,
beş seansın beşi de yakalanır. ABD dışındaki bir saat diliminde bu, kapanış saatini yerel saate
çevirmekten hem basit hem sağlam.

**macOS (launchd — önerilen).** `cron` Mac uykudayken çalışmaz ve kaçan gün kalıcı olarak
kaybolur; `launchd` kaçırdığı işi uyanınca koşturur. `~/Library/LaunchAgents/csp-snapshot.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>csp-snapshot</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/csp</string>   <!-- `which csp` ne diyorsa: cron/launchd PATH'i dar -->
    <string>--universe</string>
    <string>--capital</string><string>2000</string>
    <string>--snapshot</string>
  </array>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>9</integer></dict>
    <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>9</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>9</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>9</integer></dict>
    <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>9</integer></dict>
  </array>
  <key>StandardOutPath</key><string>/Users/KULLANICI/.csp_snap.log</string>
  <key>StandardErrorPath</key><string>/Users/KULLANICI/.csp_snap.log</string>
</dict>
</plist>
```

plist `~` genişletmiyor, log yollarını tam yaz. Sonra:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/csp-snapshot.plist
launchctl kickstart -p gui/$(id -u)/csp-snapshot   # beklemeden bir kez koştur
tail ~/.csp_snap.log                               # hangi seans kaydedildi
launchctl bootout gui/$(id -u)/csp-snapshot        # kaldırmak için
```

**Linux (cron).** Buradaki asıl tuzak PATH: cron `csp`'yi bulamaz, tam yol şart.

```
# crontab -e  (hafta içi 09:00)
0 9 * * 1-5 /usr/local/bin/csp --universe --snapshot >> ~/.csp_snap.log 2>&1
```

Toplu veri satın alırsan (OptionsDX/dolt) aynı 10 kolonluk tabloya yükle; `--replay` fark etmez.

### `--replay SYM` ne yapıyor

Kayıtlı zincirleri gün gün gezip **tarayıcının kendi filtreleriyle ve kendi skoruyla** kontrat
seçiyor, sonra vadeye (ya da `--take-profit` verdiysen kâr hedefine) kadar tutuyor. Bir portföy
simülasyonu: girişleri sınırlayan tek şey nakit.

- giriş fiyatı **kaydedilen bid** — gördüğün mid değil, gerçekten alacağın fiil
- her gün önce vadesi gelenler uzlaşıyor, sonra serbest kalan nakitle en yüksek skorlu kontratlar
  sırayla dolduruluyor; sermaye bitince durur
- aynı sembolde en fazla `--max-per-symbol` açık pozisyon (varsayılan 1), yoksa tek isim kitabın
  tamamı olabilir
- `--replay TÜMÜ` kaydettiğin bütün sembolleri tek bir kitap olarak sürüyor
- uzlaşma: vade gününün gerçek kapanışı (CBOE günlük barları), `prim×100 + min(0, S_T − strike)×100`
- RV bileşeni o güne kadarki kapanışlarla hesaplanıyor — ileriye bakış yok
- vadesi henüz geçmemiş kontrat sonuç değil: "hâlâ açık" diye ayrı raporlanıyor, P&L'e girmiyor
- erken kapatma yalnız `--take-profit` verilirsen, aşağıdaki kuralla; roll yok, wheel yok,
  kazanç filtresi yok (geçmiş kazanç tahminleri kayıtlı değil)

Özet satırları portföy diliyle konuşuyor: **aynı anda bloke edilen tepe** nakit (en büyük tek
teminat değil), kitabın **takvim günü** ömrü (boşta geçen günler dahil) ve bunun yanında
bilgi olarak pozisyon-günü. Yıllık getiri tepe teminata ve takvim gününe göre — yani boşta
duran nakit de paydada.

```
sym        giriş       vade  dte  strike   prim    spot  uzlaşma  skor    P&L$
NOK   2026-01-05 2026-02-06   32    9.00   0.25   10.00    10.00    44     +25
SOFI  2026-01-05 2026-02-06   32   17.00   0.64   18.00    18.00    49     +64
NOK   2026-02-09 2026-03-13   32    9.00   0.25   10.00    10.00    44     +25
SOFI  2026-02-09 2026-03-13   32   17.00   0.64   18.00    18.00    49     +64

4 işlem · 2 sembol · 2026-01-05 → 2026-03-13 · 67 takvim günü · 128 pozisyon-günü
toplam +$178 · işlem başına +$44 · en kötü +$25
kazanan %100 · atanan %0 · en kötü seri +$0 · aynı anda bloke edilen tepe $2600
tepe teminata göre yıllık %37.3  (boşta geçen günler dahil)
```

Satırlar **çözülme sırasında**, yani nakdin gerçekten geldiği sırada; birkaç kontrat aynı anda
açıkken bu giriş sırasından farklı oluyor ve "en kötü seri" ancak bu sırayla anlamlı.

(Yukarısı motoru gerçek NOK kotasyonlarını geçmişe kaydırarak sürdüğüm duman testi; `spot`
kolonu o yüzden bugünün spotu. Tek günlük gerçek kayıtla `--replay` haklı olarak boş dönüyor:
kayıtlı vadelerin hiçbiri henüz geçmedi.)

### `--take-profit`: kârı erken al, nakdi erken kurtar

"Primin %50'sini kazanınca kapat" en yaygın CSP kuralı, ama bir tercih değil ölçülebilir bir
soru: erken kapanınca spread'i ve kalan zaman değerini bırakıyorsun, karşılığında teminatı ve
kuyruk riskini geri alıyorsun. Elinde kayıtlı zincir varken bu soruyu **uydurmadan**
cevaplayabiliyorsun, çünkü geri alım fiyatı da o gün gerçekten var olan bir kotasyon:

- giriş kaydedilen **bid**, çıkış kaydedilen **ask** — gidiş-dönüş spread'i olması gerektiği
  kadar pahalı
- `--take-profit 0.5`: o günün kaydında kontratın ask'i primin yarısına ya da altına düştüyse
  kapanıyor. Ask `0` ise teklif yok demektir, bedava geri alım değil: pozisyon açık kalıyor
- kapanan gün teminat **o gün** serbest kalıyor ve aynı gün başka bir kontratı fonlayabiliyor —
  kuralın bütün iddiası zaten bu
- aynı kontrat kapandığı gün yeniden satılmıyor (ask'ten alıp bid'den satmak sadece spread)
- bayrak yoksa hiçbir şey değişmiyor: her kontrat vadeye taşınıyor

Tabloya `çıkış` kolonu yalnız kitapta erken kapanış varsa ekleniyor; `↩0.11` "0.11'den geri
alındı", çıplak sayı ise vade günü hissenin kapanışı:

```
sym        giriş       vade      çıkış  dte  strike   prim    spot  uzlaşma  skor    P&L$
NOK   2026-01-05 2026-02-06 2026-01-20   32    9.00   0.25   10.00    ↩0.11    43     +14
SOFI  2026-01-05 2026-02-06 2026-02-06   32   17.00   0.64   18.89    16.80    54     +44

1 pozisyon kâr hedefiyle erken kapatıldı (↩ = kaydedilen ask'ten geri alım) · 1 tanesi vadeye taşındı
```

(Bu tablo da aynı türden bir duman testi: kotasyonlar elle yazılmış, gerçek bir kayıt defteri
değil.) Aynı kitabı bayraksız koştur, iki satırı yan yana koy: pozisyon-günü, tepe teminat ve yıllık
getiri farkı kuralın o evrende neye mal olduğunu söyler. Boşta kalan nakde koyacak yeni kontrat
yoksa erken kapatmak yıllık getiriyi **düşürür** — bu motor onu da dürüstçe gösteriyor.

## Yapılmayanlar

Roll / wheel (atanan hisseyi covered call'a çevirme) simülasyonu, toplu
geçmiş zincir importer'ı
(OptionsDX/dolt CSV → SQLite), pozisyon defteri/günlük, temel-iflas riski (Merton
distance-to-default), faktör yoğunlaşma cezası, otomatik yenileme.
`--replay` bugün yalnız kendi kaydettiğin günleri görüyor: geçmiş, kaydetmeye başladığın
günden ileri doğru birikiyor.
Evren taraması CBOE'nin hız sınırına takılı: kapı seri, paralellik yalnız indirmeleri
örtüştürüyor. 5 karakterden uzun sembol tablo kolonlarını kaydırır.

## Uyarı

Bu bir araştırma aracı, yatırım tavsiyesi değil. Veri gecikmeli ve vendor'a bağlı; skor bir
önsav, `EV$` ve `--replay` geçmişe dair bir ölçüm — ikisi de gelecek getirisi vaadi değil.
Kasa-teminatlı put satmak hisseyi strike'tan almayı taahhüt etmektir; kaybın primden çok daha
büyük olabilir. Kendi kararını kendi paranla verirsin.

## Lisans

MIT — bkz. [LICENSE](LICENSE). Katkı: PR'lar açık; tek kural, `pytest` yeşil kalsın ve yeni
davranış kendi testini getirsin.
