# Mobil Fabrika Beyni ("Fabrika") — Tasarım Dokümanı

> Tarih: 18 Ağustos 2026
> Durum: TASARIM v2 — onay bekliyor
> Model: Codeway tarzı uygulama fabrikası, tek kişilik ekip + AI beyin
> Kardeş sistemler: Nöbetçi v2 (otonom operatör), blog otomasyonu, AppMetric

## 0. Vizyon ve İlkeler

Tek cümle: **Bundan sonra geliştirilen her mobil uygulama; fikirden kapanışa/ölçeğe kadar, veriyle karar veren tek bir beyin tarafından yönetilir.** Beyin ops host'ta yaşar (Nöbetçi deseninin genişletilmişi), kararlar Telegram onayından geçer, eller (executor) onaylı aksiyonları API'lerle uygular.

Codeway'den alınan çekirdek dersler (kaynak: vaka analizleri, UA podcast):
1. Gelirin %80'i abonelik (haftalık planlar Tier-1'de kritik) — paywall en önemli ekran.
2. Büyüme ücretli reklamdan gelir; ASO destekleyicidir, motor değildir.
3. Endüstriyel test: ayda binlerce kreatif, gün-1'den metrik kıyası, acımasız öldürme.
4. Başabaş eşiği LTV:CAC ≈ 1.2; TR devlet teşvikiyle (%70 reklam iadesi) fiilen ~1:2.
5. Portföy mantığı: tek uygulamaya âşık olma; sermayeyi kazanana kaydır.

İlkeler:
- **Standart doğum:** her uygulama aynı şablondan, aynı event sözleşmesiyle doğar. Beyin özel durum sevmez.
- **Blended ölçüm önce:** ATT sonrası dünyada kanal-içi atıf yalan söyler; ana karar metriği ülke×gün blended CAC vs LTV. SKAN/AEM destekleyici sinyal. MMP başta YOK (maliyet + tek kişi), 3+ uygulama ölçeğinde Tenjin/AppsFlyer değerlendirilir.
- **Geri-alınabilirlik kapısı:** Nöbetçi'deki reversibility sınıflandırması aynen: geri alınabilir aksiyonlar (bütçe ±, kreatif aç/kapat, paywall varyantı) hızlı onay; geri alınamazlar (uygulama kill, fiyat kalıcı değişimi, store metadata) açık onay + soğuma süresi.
- **Örneklem disiplini:** hiçbir deney minimum örneklem dolmadan "kazandı" ilan edilmez (bkz. feedback_check_sample_size). Beyin her deney raporunda n ve güven düzeyi yazar.

## 1. Genel Mimari

```
┌─────────────────────────── OPS HOST (164.90.179.52) ───────────────────────────┐
│                                                                                 │
│  CRON ──► ETL (fabrika-etl.ts)          ──► factory DB (Postgres, DO managed)   │
│  CRON ──► BEYİN (claude -p, playbook'lu) ──► kararlar + brifing                 │
│                │                                                                │
│                ▼                                                                │
│        POST /api/ops/propose (mevcut) ──► ops_actions ──► Telegram onay         │
│                                                              │                  │
│                                                              ▼                  │
│                                          EXECUTOR (allowlist'li eller)          │
│                                          ├─ Adapty API (paywall/fiyat/A-B)      │
│                                          ├─ Meta Marketing API (bütçe/kreatif)  │
│                                          ├─ TikTok Ads API                      │
│                                          ├─ Apple Search Ads API                │
│                                          ├─ fastlane (store metadata)           │
│                                          └─ Kreatif hattı (görsel/video üret)   │
└─────────────────────────────────────────────────────────────────────────────────┘

VERİ KAYNAKLARI: Adapty (gelir gerçeği) · Firebase→BigQuery (funnel/retention)
· Meta/TikTok/ASA raporlama API (harcama, kreatif metrikleri) · App Store Connect
& Play Console API (indirme, sayfa dönüşümü, review) · AppMetric (pazar/rakip/ASO)
```

Beyin = tek bir uzun sistem promptu değil; **playbook dosyaları** (repo'da markdown) + günlük veri özeti ile beslenen `claude -p` koşusu. DT runbook deseni: her uyanışta playbook'u okur, durumu değerlendirir, karar önerir, durum makinesine yazar.

## 2. Modüller

### M0 — Veri Omurgası (factory DB)

Ayrı şema (`factory.*`), mevcut DO Postgres'te. Çekirdek tablolar:

```
apps(id, name, bundle_id, platformlar, durum: idea|build|soak|spark|scale|harvest|killed,
     dogum_tarihi, sablon_versiyonu, adapty_app_id, firebase_project, notlar)

app_metrics_daily(app_id, tarih, ulke, platform, kanal,           -- kanal: meta|tiktok|asa|organic|blended
     harcama, gosterim, tik, install, trial, odeme, iptal,
     gelir_brut, gelir_net, iade, arpu_d0, ret_d1, ret_d7, ret_d28)

creatives(id, app_id, tip: video|gorsel|playable, hook_kodu, format_kodu, stil_kodu,
     uretim: ai|manuel, asset_url, durum: taslak|onaylandi|canli|emekli, dogum)

creative_metrics_daily(creative_id, tarih, kanal, harcama, gosterim, tik, install,
     trial, ctr, cpi, cpt, frekans)

experiments(id, app_id, tip: paywall|fiyat|onboarding|kreatif|aso, hipotez,
     varyantlar jsonb, durum, baslangic, bitis, min_n, sonuc jsonb, karar)

decisions(id, app_id, tip, oneri jsonb, kanit jsonb, ops_action_id FK,
     durum: onerildi|onaylandi|reddedildi|uygulandi, tarih)

ideas(id, kaynak: appmetric|trend|manuel, baslik, skor jsonb, durum, notlar)

incentive_ledger(id, app_id, tip: reklam_iadesi|komisyon_iadesi|maas,
     donem, uygun_harcama, beklenen_iade, durum: birikmis|basvuruldu|alindi)
```

### M1 — Fikir & Doğrulama (Discovery)

**Girdi:** AppMetric (kendi ürünümüz — kategori büyümesi, rakip gelir tahmini, keyword hacimleri), App Store/Play yükselen listeleri, trend sinyalleri.
**Süreç (haftalık cron):** beyin aday fikirleri 6 eksende puanlar:

| Eksen | Soru | Ağırlık |
|---|---|---|
| Pazar hacmi | Kategori aylık indirme + gelir (AppMetric) | 0.20 |
| Rekabet boşluğu | Lider zayıf mı, review şikayetleri ne | 0.20 |
| Abonelik uyumu | Haftalık/yıllık abonelik doğal mı | 0.20 |
| UA maliyet tahmini | Benzer app CPI benchmark'ı | 0.15 |
| Yapım maliyeti | Şablonla kaç haftada MVP | 0.15 |
| Kreatif potansiyeli | TikTok'ta gösterilebilir mi (demo-ability) | 0.10 |

**Çıktı:** `ideas` tablosuna skorlu brief; eşik üstü fikirler haftalık Telegram brifingine düşer. Karar (yap/yapma) HER ZAMAN insanda.

### M2 — Uygulama Şablonu (Factory SDK)

Flutter starter repo (`factory-app-template`). Her uygulama bununla doğar:

- **Monetizasyon:** Adapty SDK — abonelik, paywall remote config, A/B testi, no-code paywall builder. (RevenueCat alternatifti; Adapty seçildi: paywall A/B + builder yerleşik, fiyat testi API'den yönetilebilir → beynin "elleri" için kritik.)
- **Analitik:** Firebase Analytics + BigQuery export (ham event erişimi şart, Firebase UI yetmez). Crashlytics.
- **Standart event sözleşmesi (v1):**

```
app_open, att_prompt_view, att_grant, att_deny,
onboarding_start, onboarding_step{n}, onboarding_complete,
paywall_view{yer: onboarding|feature|settings}, paywall_dismiss,
discount_view, discount_accept,                    -- oyunlaştırılmış indirim ekranı
trial_start, trial_convert, purchase{urun}, cancel, refund,
core_action{ad},                                   -- uygulamanın "değer anı" eylemi
rating_prompt_view, rating_prompt_accept
```

- **Paywall akışı (Codeway deseni, şablonda hazır):** onboarding (3-5 ekran, değer vaat) → hard-ish paywall (kapat X'i 2sn gecikmeli) → kapatana **oyunlaştırılmış indirim** (çark/kazı-kazan, %40-50 indirimli yıllık) → yine kapatana sınırlı free mod.
- **Fiyat iskeleti (Tier-1 varsayılan, Adapty'de ülke bazlı override):** haftalık $4.99-6.99 (3 gün trial) · yıllık $29.99-49.99 (varsayılan seçili, %50-60 indirim algısı) · aylık YOK (haftalık+yıllık kutuplaması bilinçli).
- **Diğer:** remote config (force update, feature flag), derin link, ATT metni şablonu, review isteme "mutlu an" kancası, fastlane konfigürasyonu (metadata/screenshot deploy).

Vakt bu şablona taşınır → fabrikanın ilk ürünü ve pilot verisi.

### M3 — Ölçüm Katmanı (ETL)

Günlük cron (06:00 UTC), `scripts/factory/etl.ts`:

| Kaynak | Çekilen | Not |
|---|---|---|
| Adapty API | gelir, trial, ödeme, iptal, iade, ürün bazında | **Gelir gerçeğinin tek kaynağı** |
| BigQuery (Firebase) | funnel eventleri, retention kohortları | SQL şablonları repo'da |
| Meta Marketing API | harcama, gösterim, tık, install (SKAN), kreatif bazında | |
| TikTok Ads API | aynı | |
| Apple Search Ads API | harcama, tap, install, keyword bazında | |
| App Store Connect API | indirme, sayfa görüntüleme→indirme dönüşümü, review'lar | |
| Play Console API | aynı + listing deney sonuçları | |
| AppMetric | rakip sıralama, keyword pozisyonları | ASO modülüne |

Hepsi `app_metrics_daily` + `creative_metrics_daily`'ye normalize edilir. **Blended hesap:** ülke×gün bazında toplam harcama / toplam yeni ödeme = gerçek CAC; Adapty kohort geliri → gerçek LTV. Kanal atfı yön göstergesi, blended hakem.

### M4 — Ekonomi Modeli (unit economics)

Beynin her kararının altında yatan hesap:

- **LTV tahmini (basit → kalibre):** LTV₂₈ = kohortun 28 günlük net geliri; LTV_proj = LTV₂₈ × k (k başlangıçta 2.0, her uygulamada gerçek verilerle aylık yeniden kalibre edilir; haftalık abonelikte churn hızlı → k'yı iyimser tutma).
- **Karar eşikleri (varsayılan, playbook'ta güncellenir):**

| Metrik | Zayıf | Kıvılcım | Güçlü |
|---|---|---|---|
| Onboarding tamamlama | <50% | 60-75% | >75% |
| Paywall görüntüleme (install'a) | <60% | 70-85% | >85% |
| Trial başlatma (install'a, T1 iOS) | <3% | 5-8% | >10% |
| Trial→paid | <25% | 35-45% | >50% |
| D1 retention (utility) | <20% | 25-35% | >40% |
| Blended LTV:CAC (28g proj.) | <0.8 | 1.0-1.2 | >1.5 |
| CPI (T1 iOS utility) | >$4 | $2-4 | <$2 |

- **Teşvik muhasebesi ayrı satır:** eşikler teşviksiz hesaplanır (ürün gerçeği), teşvikli görünüm (`incentive_ledger`) nakit akışı planında gösterilir. Teşvik ürün zayıflığını maskelemesin.

### M5 — UA / Kampanya Beyni

**Yaşam döngüsü kampanya planı:**

1. **SOAK (soft launch, 1-2 hafta):** TR + 1 ucuz İngilizce pazar (PH/ZA), günlük $20-50. Amaç: teknik sağlık + funnel şekli. Gate: crash-free >99.5%, onboarding >60%, paywall view >70%.
2. **SPARK (2-4 hafta):** US iOS, günlük $50-150, Meta (1 kampanya, broad, 4-6 kreatif) + TikTok paralel. Amaç: kıvılcım metrikleri (M4 tablosu). Gate: en az 2 metrik "kıvılcım", LTV:CAC projeksiyonu ≥1.0. Toplam test bütçesi tavanı: **$2.000-3.000/uygulama** — dolduğunda kıvılcım yoksa KILL önerisi otomatik düşer.
3. **SCALE:** LTV:CAC ≥1.2 iki hafta üst üste → haftalık bütçe +%25-30 artış önerisi; kanal çeşitlendirme (ASA ekle, sonra Google). Ülke genişlemesi: US → CA/UK/AU → DE/FR (lokalize paywall şart).
4. **HARVEST:** büyüme platoya ulaştı → harcama ROAS-nötr seviyeye çekilir, nakit portföye döner.

**Günlük otomatik kurallar (beyin önerir, onayla uygulanır; Faz D'de ön-onaylı):**
- Ad set: harcama > 3×hedef CPA ve 0 trial → durdur.
- Kreatif: CTR 3 günlük ort. < kampanya ort. ×0.6 VEYA frekans >4 → emekli et, yerine yeni varyant.
- Bütçe: son 3 gün CPA ≤ hedef ×0.85 → +%20; ≥ hedef ×1.3 → −%30.
- **Harcama bekçisi (saatlik cron, beyinden bağımsız):** günlük tavan aşımı → kampanyaları durdur önerisi ACİL etiketiyle; ayrıca platform tarafında da hard cap tanımlı (çift emniyet).

### M6 — Kreatif Fabrikası

Codeway ayda 6.000 kreatif test ediyor; bizim ölçek haftada 10-30 varyant, ama aynı sistematikle:

- **Hook matrisi:** her uygulama için `playbooks/{app}/hooks.md` — acı noktası × format × stil kombinasyonları. Örn. Vakt: "namazı kaçırma korkusu × POV video × duygusal", "kıble bulamama × ekran kaydı demo × pratik".
- **Üretim hattı (executor aksiyonları):**
  - *Görsel/statik:* üretken görsel API (gpt-image / Flux) → şablon overlay (başlık + CTA) → 3 boyut varyantı (1:1, 9:16, 4:5).
  - *Video:* **Remotion** (kod-bazlı video, tam otomasyona uygun) ile şablon aileleri: ekran kaydı + altyazı + hook metni; stok/AI b-roll + TTS (ElevenLabs); UGC-stil AI avatar (HeyGen/Captions API) — SPARK sonrası, maliyetli.
  - Her asset `creatives` tablosuna hook/format/stil koduyla yazılır → performans verisi koda bağlanır → **beyin hangi hook'un kazandığını öğrenir** ve sonraki üretim brief'ini kazanan yönde eğer.
- **Onay akışı:** üretilen batch Telegram'a albüm olarak düşer → onaylananlar Marketing API ile taslak reklama yüklenir. (Reklam YAYINI her zaman onaylı aksiyon.)
- **Politika kontrolü:** beyin her kreatifi platform reklam politikalarına (sağlık iddiası, before/after, din hassasiyeti vb.) karşı ön-kontrol eder; riskli olanı işaretler.

### M7 — ASO Modülü

ASO destek motorudur (Codeway dersi) ama bedava trafik bedava:

- **Keyword takibi:** AppMetric pozisyon verisi → haftalık delta raporu; hedef keyword seti `playbooks/{app}/aso.md`.
- **Metadata iterasyonu:** 2-4 haftada bir başlık/altbaşlık/keyword alanı önerisi (arama hacmi × alaka × zorluk skoru ile); uygulama fastlane üzerinden onaylı aksiyon.
- **Görsel deneyler:** Apple Product Page Optimization + Play listing experiments — ikon/screenshot varyantları kreatif fabrikasından beslenir; sonuçlar `experiments`'a.
- **Rating döngüsü:** şablondaki "mutlu an" prompt'u (core_action sonrası) + haftalık review madenciliği: beyin tüm yeni review'ları okur → duygu + tema özeti + ürün backlog önerisi (Codeway'in "bölüm ortası rating isteği" deseni şablonda hazır).
- **ASA senkronu:** organik sıralama kazanılan keyword'lerde ASA teklifini düşür, kaybedilende savunma teklifi — öneri olarak.

### M8 — Analiz & Yorum (Analist)

**Günlük brifing (07:00 UTC, Telegram):** uygulama başına tek kart: harcama, gelir, blended ROAS, trial akışı, dünden sapmalar (>%25 sapma 🔴/🟢 işaretli), açık deneylerin n/durumu, bekleyen karar sayısı. Portföy toplamı en üstte.
**Haftalık portföy incelemesi (Pazartesi):** LTV:CAC trendi uygulama bazında; sermaye tahsisi önerisi ("X'ten $Y bütçeyi Z'ye kaydır"); kreatif lig tablosu (hook bazında); ASO delta; fikir hattından yeni adaylar; teşvik defteri durumu.
**Anlık anomali:** harcama bekçisi (M5) + gelir çöküşü (bugün gelir < 7g ort ×0.5 → araştır ve bildir).
**Deney raporu:** her deney kapanışında: hipotez → sonuç → n → güven → karar → playbook'a işlenecek ders. Ders playbook dosyasına **beyin tarafından PR ile** eklenir (kurumsal hafıza).

### M9 — Karar Motoru & Playbook'lar

Repo yapısı:

```
factory/
  playbooks/
    core.md            # yaşam döngüsü, eşikler (M4/M5 tabloları), karar kuralları
    creative.md        # hook matrisi metodolojisi, platform politika notları
    aso.md             # metadata iterasyon kuralları
    {app}/             # uygulama-özel: hooks.md, aso.md, notes.md
  prompts/
    daily-brain.md     # günlük koşu promptu
    weekly-review.md
  scripts/
    etl.ts, spend-guard.ts, brain-run.sh
```

Beyin koşusu = `claude -p` + playbook + son N gün veri özeti (ETL'den JSON). Çıktı: yapılandırılmış karar listesi → `POST /api/ops/propose`. Nöbetçi'nin durum makinesi, Telegram butonları, executor allowlist'i **aynen genişletilerek** kullanılır (yeni aksiyon tipleri: `adapty_*`, `meta_*`, `tiktok_*`, `asa_*`, `fastlane_*`, `creative_*`).

### M10 — Yönetişim & Emniyet

- Faz 0 kuralı: **her aksiyon Telegram onaylı.** Otomasyon güven kazandıkça geri-alınabilir küçük aksiyonlar (bütçe ±%20'ye kadar, kreatif rotasyonu) ön-onaylı sınıfa alınır — bu geçiş de açık kararla olur.
- Harcama tavanları üç katmanlı: platform hard cap + saatlik bekçi + beynin günlük kontrolü.
- Kill-switch: `/root/factory_kill` (Nöbetçi'dekiyle aynı desen) — varsa beyin salt-rapor moduna düşer.
- Geri alınamaz aksiyonlar (app kill, kalıcı fiyat değişimi, store metadata): 24 saat soğuma + ikinci onay.
- Beyin ASLA: ödeme bilgisi girmez, hesap açmaz, platform kural ihlali önermez (teşvik/politika uyumu playbook'ta).

### M11 — Teşvik Modülü (Türkiye avantajı)

- E-Turquality / Ticaret Bakanlığı mobil uygulama destekleri: %70'e varan yurt dışı reklam iadesi, mağaza komisyon iadesi, istihdam destekleri. (Güncel şartlar başvuru öncesi doğrulanacak — mevzuat değişken.)
- `incentive_ledger`: ETL her ay uygun harcamayı işaretler, beklenen iadeyi hesaplar; brifingde "birikmiş teşvik alacağı" satırı; başvuru dönemlerinde hatırlatma.
- Belge disiplini: fatura/kampanya raporu arşivi R2'ye (başvuru kanıtları).

## 3. Uygulama Fazları

| Faz | Kapsam | Çıktı | Tahmini efor |
|---|---|---|---|
| **A — Şablon + Omurga** | factory DB şeması, Flutter şablonu (Adapty+Firebase+event sözleşmesi+paywall), Vakt'ın şablona taşınması | Ölçülebilir ilk uygulama | 1-2 hafta |
| **B — ETL + Analist (salt okunur)** | M3 ETL, M8 günlük brifing + haftalık inceleme, harcama bekçisi | Telegram'da yaşayan analist | 1 hafta |
| **C — Eller (onaylı aksiyon)** | ops_actions genişletme: Adapty paywall/fiyat, Meta/TikTok bütçe-kreatif, deney motoru | Beyin karar verir, sen onaylarsın | 1-2 hafta |
| **D — Kreatif Fabrikası** | Remotion hattı, görsel API, hook matrisi, Telegram onay albümü | Haftada 10-30 otomatik kreatif | 2 hafta |
| **E — Discovery + ASO + Teşvik** | AppMetric entegrasyonu, fikir puanlama, ASO döngüsü, incentive_ledger | Tam fabrika döngüsü | 2 hafta |

Fazlar sıralı ama B'den itibaren her faz tek başına değer üretir. İlk gerçek test: Vakt'ın SOAK→SPARK koşusu (Faz B sonunda başlayabilir).

## 4. Gereksinimler (kullanıcıdan)

- Hesap/anahtar: Adapty, Firebase+BigQuery, Meta Business + Marketing API, TikTok Ads, Apple Search Ads, App Store Connect API key, Play Console servis hesabı, görsel/video API'leri (OpenRouter-uyumlu tercih), ElevenLabs (opsiyonel), HeyGen (Faz D sonrası opsiyonel).
- Kararlar: ☐ Adapty onayı (RevenueCat yerine) ☐ test bütçesi tavanı/uygulama ($2-3K önerisi) ☐ günlük harcama tavanı ☐ Vakt pilot mu, yeni fikir mi önce?
- Teşvik başvurusu: şirket türü/ihracat kaydı durumu (E-Turquality ön şartları).

## 5. V2 — "Muhteşem" Seviye Genişletmeleri (M12–M22)

> V1 (M0-M11) fabrikayı kurar. V2, fabrikayı dünya klasmanına taşır: parayı kod yazmadan
> önce test etmek, kararı 28 gün yerine 3 günde vermek, kreatifleri evrimle üretmek,
> her uygulamanın bir sonrakini akıllandırması. V2 modülleri Faz C+'dan itibaren sırayla devreye girer.

### M12 — Fikir Doğrulama: Kod Yazmadan Para Testi (fake-door)

En pahalı hata yanlış uygulamayı yapmaktır. Fikir "yap" kararı almadan ÖNCE:
1. **Duman testi:** uygulama yokken 3-5 kreatif + basit landing (veya App Store'da "pre-order" sayfası) → $150-300 Meta/TikTok harcaması → CTR, CPC, landing'de "fiyat gör→e-posta bırak" oranı ölçülür.
2. **Eşik:** CTR < kategori medyanı VEYA intent oranı <%8 → fikir yapılmadan ölür. Kazanan fikir zaten test edilmiş hook'larıyla ve ölçülmüş CPI tahminiyle doğar — SPARK fazına yarı yoldan başlar.
3. Duman testi altyapısı şablonlaşır: landing jeneratörü (Next.js tek şablon, mevcut app sunucusunda), test kreatif seti kreatif fabrikasından.

Codeway'in "gün-1 kıvılcım" disiplininin bir adım öncesi: **gün -30 kıvılcımı.**

### M13 — Erken LTV Tahmini (pLTV): 28 Gün Değil 3 Gün

V1'de ölçek kararı 28 günlük kohort bekler — sermaye yavaş döner. V2:
- **Erken sinyal modeli:** D0-D3 davranışlarından (ilk oturumda trial başlatma, onboarding hızı, core_action sayısı, ülke, kanal, kreatif hook'u) 28 günlük LTV tahmini. Başlangıçta basit lojistik/oran tablosu (segment bazlı dönüşüm katsayıları), 5K+ kullanıcı verisi birikince gradient boosting (tek script, scikit-learn, ops host'ta).
- **Yenileme eğrisi LTV'si:** k-katsayısı yerine gerçek yenileme olasılık zinciri: LTV = Σ fiyat×(yenileme₁×yenileme₂×...) — haftalık aboneliklerde 4-6 yenileme dönemi gözlemi yeterli, aylık/yıllıktan çok daha hızlı kalibre olur (haftalık planın gizli avantajı).
- **Sonuç:** bütçe kararları D3'te, güvenle. Sermaye devir hızı 5-8× artar — küçük bütçeyle Codeway temposu tutturmanın tek yolu bu.

### M14 — Kreatif Genetiği: Evrimle Üretim + Rakip İstihbaratı

V1 hook matrisi statikti. V2'de kreatif üretim **evrimsel döngü**:
- **Kreatif DNA:** her varyant gen setiyle etiketlenir: hook (ilk 3 saniye mesajı) × görsel stil × format × tempo × CTA × ses. Performans gen bazında ayrıştırılır ("kazanan reklam" değil "kazanan gen": örn. 'korku hook'u + ekran kaydı' kombinasyonu CTR'ı 2× yapıyor).
- **Mutasyon kuralı:** her yeni batch = %60 kazanan genlerin yeni kombinasyonları + %30 tek-gen mutasyonu + %10 vahşi deneme. Batch'ler küçük ve sık (haftada 2×10, ayda 1×80 değil).
- **Rakip reklam istihbaratı:** Meta Ad Library + TikTok Creative Center top-ads haftalık taranır (kategori bazlı); uzun süredir dönen rakip reklamı = kanıtlanmış hook → DNA havuzuna "esinlenme geni" olarak girer. (Codeway lider olduğu için rakibe az bakar; biz challenger'ız, bakarız.)
- **İlk-3-saniye disiplini:** video şablonları hook'u ilk 3 saniyeye zorlar; beyin thumb-stop oranını (3sn izlenme/gösterim) ayrı metrik olarak izler — CTR'dan önce gelen öncü gösterge.

### M15 — Fiyat Bilimi: PPP, Kurtarma Teklifleri, Web Funnel

- **Satın alma gücü fiyatlaması (PPP):** tek fiyat × tüm dünya = para masada. Ülke matrisi: T1 tam fiyat, T2 %40-60, T3 %70-80 indirimli (Adapty ülke bazlı ürün seti). Big Mac endeksi tablosu playbook'ta; beyin ülke bazlı trial→paid + iade oranına göre ince ayar önerir.
- **Kurtarma teklifleri zinciri (çoğu stüdyonun bile yapmadığı):**
  - *İptal-anı:* iptal niyetinde %50 indirimli 3 ay teklifi (cancel-flow survey + offer).
  - *Trial bitişi kaçıranlara:* 24-48 saat sonra push/e-posta ile indirimli geri dönüş.
  - *Churn winback:* iptalden 7/30 gün sonra kişiselleştirilmiş teklif.
  - *Ödeme kurtarma:* billing retry + grace period (Adapty yönetir; Codeway Paddle'la $500K kurtardı — mağaza içinde de aynı disiplin).
- **Web-to-app funnel (2026'nın en büyük kaldıracı):** quiz tarzı web onboarding (Noom/BetterMe deseni) → web'de Paddle/Stripe checkout → app'e giriş. Mağaza komisyonu %30→%5'e iner, ATT körlüğü olmadan tam atıf, e-posta sahipliği. ABD'de external purchase link serbestisi ile app içinden de web checkout'a çıkış. Faz E+ işi; İhale Metrik'teki Paddle tecrübesi doğrudan transfer olur.

### M16 — Retention/CRM Motoru: Bildirim = İkinci UA Kanalı

Push bildirimi bedava trafiktir; V1'de eksikti:
- **Yaşam döngüsü kampanya şablonları** (şablon SDK'sına gömülü, remote config'le yönetilir): D0 aktivasyon serisi → trial D-1 "yarın bitiyor" → değer anı kutlaması ("bu hafta 3 hedef tamamladın") → uykuya dalan kullanıcıya D7/D14 geri çağırma → churn winback.
- Beyin bildirim performansını (açılma→dönüşüm) kreatif gibi test eder: metin varyantları, gönderim saati (kullanıcının aktif saati), frekans tavanı (haftada ≤3, bezdirme=uninstall).
- E-posta katmanı (web funnel gelince): Resend zaten stack'te.

### M17 — Yayınsız İterasyon: Remote-Config Onboarding + OTA

Fabrikanın hız sınırı store review'dur (1-3 gün). Kaldırma yolu:
- **Onboarding ve paywall tamamen remote-config-sürümlü:** ekran sırası, metinler, görsel seçimi, soru akışı JSON'dan çizilir → beyin onboarding A/B testini **yayın yapmadan** kurar. (Adapty paywall'da bunu zaten verir; onboarding için şablona `flow_config` motoru yazılır.)
- **Shorebird (Flutter OTA code push):** Dart seviyesi yamalar dakikalar içinde canlıya — kritik bug/paywall değişikliği store beklemez.
- Sonuç: Codeway'in haftalık iterasyon temposu, tek kişiyle.

### M18 — Reklam↔Mağaza Uyumu: Custom Product Pages

Kullanıcı "sırt ağrısı" reklamına tıklayıp jenerik store sayfası görürse dönüşüm düşer:
- **Apple Custom Product Pages (35 adede kadar) + Google Custom Store Listings:** her ana hook temasına özel mağaza sayfası (screenshot seti kreatif fabrikasından) → reklam kampanyası ilgili CPP'ye bağlanır.
- Beyin CPP bazlı sayfa dönüşümünü izler; kazanan CPP varsayılan sayfaya terfi önerilir.
- ASA'da keyword→CPP eşlemesi (arama niyetiyle sayfa mesajı hizalanır).

### M19 — Lokalizasyon Fabrikası

T1 doyunca büyüme lokalizasyondan gelir; LLM çağında bu ucuzladı:
- **Boru hattı:** app metinleri (arb/strings) + store listing + paywall + kreatif altyazıları → LLM çeviri (bağlam sözlüğüyle) → native-check örneklemi → fastlane ile yükleme. Yeni dil eklemek = 1 onay.
- **Sıralama:** DE/FR/ES/PT-BR/JA ilk dalga (ödeme gücü × rekabet boşluğu skoru); kreatifler TTS ile yerel seslendirilir.
- Dini/kültürel hassasiyet kontrolü beyin ön-taramasında (M6 politika kontrolüyle birleşik).

### M20 — Sanal Büyüme Ekibi: Uzman Ajanlar

Tek dev beyin koşusu yerine **rol bazlı ajan orkestrası** (mevcut otonom-agent planıyla uyumlu):

| Ajan | Görev | Tetik |
|---|---|---|
| **UA Analisti** | kampanya/kreatif metrikleri, bütçe önerileri | günlük |
| **Kreatif Direktör** | DNA analizi, batch brief'i, rakip reklam taraması | haftada 2 |
| **Ürün Analisti** | funnel/retention/deney sonuçları, feature önerisi | günlük |
| **ASO Uzmanı** | keyword/CPP/review madenciliği | haftalık |
| **CFO** | portföy P&L, nakit akışı, teşvik defteri, sermaye tahsisi | haftalık |
| **Başhakem (orkestratör)** | ajan çıktılarını birleştirir, çelişkileri çözer, TEK brifing + karar listesi üretir | günlük |

Her ajan kendi playbook'u + kendi veri kesitiyle koşar (küçük bağlam = keskin analiz); Başhakem sentezler. Telegram'a hâlâ TEK brifing düşer — sana çok ses değil, tek net ses ulaşır.

### M21 — Gölge Mod + Karar Kalitesi Ölçümü (beynin karnesi)

Beyne güç vermeden önce ve verdikten sonra beynin KENDİSİ ölçülür:
- **Gölge mod:** Faz C'nin ilk 2-4 haftası beyin kararları "uygulasaydım ne olurdu" defterine yazılır (paper-trading). Öneri isabeti (örn. "bu kreatif yorulacak" dedi → gerçekten yoruldu mu?) ölçülür; isabet eşiği geçilince aksiyon yetkisi açılır.
- **Karar günlüğü kalıcı:** her uygulanan kararın 7/28 gün sonrası gerçekleşen etkisi karara işlenir → beynin hangi karar tipinde güçlü/zayıf olduğu raporlanır → zayıf tiplerde onay eşiği yükselir, güçlülerde ön-onay genişler. **Otomasyon güveni veriyle kazanılır, varsayımla değil.**

### M22 — Bileşik Bilgi Çarkı: Fabrika Hafızası

Codeway'in gerçek hendeği 6 yıllık kurumsal öğrenme. Bizim versiyonu:
- **Ders defteri (`factory/learnings/`):** her deney, her kill, her ölçek kararından çıkan ders yapılandırılmış kayıt (bağlam → gözlem → kural). Beyin her koşuda ilgili dersleri okur.
- **Kategori benchmark'ları:** her uygulamadan toplanan metrikler kategori bazlı benchmark tablosuna akar → 3. uygulamanın SPARK değerlendirmesi 1.'nin gerçek verisiyle yapılır, sektör ortalaması tahminiyle değil.
- **Şablon geri-besleme:** kazanan paywall/onboarding desenleri şablonun yeni varsayılanı olur → **her uygulama bir öncekinden daha güçlü doğar.** 5. uygulamada fabrika, ilk günkü fabrikanın 2-3 katı isabetle çalışıyor olmalı; bu çark V2'nin bileşik faizi.

### M23 — Çapraz Tanıtım Ağı (portföyün bedava trafiği)

Portföy 2+ uygulamaya ulaşınca uygulamalar birbirinin reklam kanalı olur (Codeway da yapar):
- Şablonda `cross_promo` yüzeyi (ayarlar/onboarding-sonu kartı, remote-config'ten hangi kardeş uygulamanın gösterileceği yönetilir).
- Beyin eşleşmeyi kitle örtüşmesine göre seçer (namaz app kullanıcısına zikir app'i, İngilizce öğrenene kelime app'i); install atfı deep-link parametresiyle `app_metrics_daily`'ye `kanal=cross_promo` olarak düşer.
- CAC = $0; ölçekte portföyün kendisi bir dağıtım ağına dönüşür — fabrikanın ağ etkisi.

### V2 Revize Faz Planı

| Faz | V1 içeriği | V2 eklentisi |
|---|---|---|
| A — Şablon+Omurga | şablon, factory DB, pilot onboarding | M17 flow_config motoru + Shorebird şablonda baştan |
| B — ETL+Analist | ETL, brifing, bekçi | M13 basit pLTV tablosu, M21 karar günlüğü altyapısı |
| C — Eller | onaylı aksiyonlar, deneyler | **M21 gölge mod zorunlu kapı**, M16 bildirim şablonları |
| D — Kreatif | Remotion hattı, hook matrisi | M14 kreatif DNA + rakip taraması, M18 CPP üretimi |
| E — Discovery+ASO | AppMetric, fikir puanı, teşvik | **M12 fake-door duman testi**, M15 PPP + kurtarma zinciri |
| F — Ölçek | — | M15 web-to-app funnel, M19 lokalizasyon fabrikası, M20 ajan orkestrası, M13 ML pLTV |

## 6. Açık Sorular / Riskler

- **Tek kişi darboğazı:** fabrika kreatif onayı + Telegram kararlarıyla bile günde ~15-30 dk ister; brifing tasarımı bunu aşmamalı.
- **Apple/Google politika riski:** agresif paywall desenlerinde (gecikmeli X, indirim çarkı) red riski — şablonda "policy-safe" varyant da bulunur.
- **SKAN körlüğü:** kanal bazlı optimizasyon sınırlı; blended disiplinden sapılmamalı.
- **AppMetric veri tazeliği:** discovery modülü AppMetric toplayıcılarının sağlığına bağımlı.
- **k katsayısı riski:** LTV projeksiyonu ilk aylarda kördür; SCALE kararları ilk 2 uygulamada muhafazakâr eşikle (≥1.5) alınmalı.
