# Fabrika Playbook — Çekirdek (v0)

> Beynin her koşuda okuduğu kural seti. Ders çıktıkça bu dosya GÜNCELLENİR
> (M22 — her güncelleme commit'li, gerekçeli). Tasarım: docs/superpowers/specs/2026-08-18-mobil-fabrika-beyni-design.md

## Yaşam döngüsü ve kapılar

`idea → build → soak → spark → scale → harvest` (her an → `killed`)

| Kapı | Şart | Bütçe |
|---|---|---|
| SOAK → SPARK | crash-free >99.5%, onboarding tamamlama >60%, paywall görüntüleme >70% | $20-50/gün, TR+ucuz EN pazarı |
| SPARK → SCALE | ≥2 "kıvılcım" metriği + blended LTV:CAC ≥1.0 (28g proj.) | $50-150/gün US iOS; tavan $2-3K |
| SPARK → KILL | test bütçesi doldu, kıvılcım yok | otomatik öneri düşer |
| SCALE kuralı | LTV:CAC ≥1.2 iki hafta üst üste → haftalık bütçe +%25-30 | |

## Kıvılcım eşikleri (utility/T1 iOS varsayılanları — gerçek veriyle kalibre edilecek)

| Metrik | Zayıf | Kıvılcım | Güçlü |
|---|---|---|---|
| Onboarding tamamlama | <50% | 60-75% | >75% |
| Trial başlatma (install'a) | <3% | 5-8% | >10% |
| Trial→paid | <25% | 35-45% | >50% |
| D1 retention | <20% | 25-35% | >40% |
| CPI (Meta, US iOS) | >$4 | $2-4 | <$2 |

## ParentLaw'a özel notlar

- Kategori "mecburiyet" kategorisi (velayet/mahkeme): haftalık plan YOK, aylık/yıllık doğru.
  Tam-dolar fiyat felsefesi KORUNUR (.99 yasak — kod yorumlarında gerekçeli).
- Abonelik HANE bazlı (kullanıcı değil) — funnel'da payer bir kişi, değer iki kişiye.
- Trial: mağaza yönetimli (Apple intro offer + Play 'trial' etiketli offer).
  Backend `trialing` durumu yazar; trial→paid = trialing→active geçişi.
- Rakip çapası paywall'da: OurFamilyWizard $220-600/yıl, TalkingParents $168-768/yıl.

## Karar disiplini

- Örneklem dolmadan "kazandı" ilan edilmez; her deney raporu n + güven yazar.
- Blended hakem: platform install sayısı yön göstergesi, gelir gerçeği backend DB.
- Teşvik (E-Turquality) eşiklere DAHİL EDİLMEZ; ayrı defterde raporlanır.
