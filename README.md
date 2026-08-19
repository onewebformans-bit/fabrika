# Mobil Fabrika Beyni

Codeway tarzı uygulama büyütme fabrikası: ben sadece uygulamayı kodlarım,
fabrika ölçüm, mağaza, ASO, kreatif, reklam ve kota kararlarını yürütür.
Panel: https://fabrika.rixe.ai (gizli yol jetonlu).

```
Uygulama sunucuları ──yaz──►  factory şeması (PG, droplet 157.230.191.204)
  collect-*.py cron'ları        app_metrics_daily · app_status · app_inputs
                                      ▲                    │
Ops host (164.90.179.52) ────oku──────┘                    ▼
  brain.py (claude -p, 06:45)                panel/generate.py → statik HTML
                                             web.py → panel yazma API'si
```

## Yerleşim (düz — kod kökte)

- `factory.py` — giriş kapısı: `add | needs | status | run | list`
- `apps/*.yaml` — künyeler (uygulama başına tek manifest; sabit 6'lı intake)
- `gates.py` + `test_gates.py` — kota/kapı motoru: CONTINUE/SCALE/HOLD/KILL
- `pipeline.py` — 10 aşamalı boru hattı (instrument→…→watch), gerçeklik bazlı kontroller
- `collect-appstore.py` (00:15) · `collect-google.py` (00:25) · `collect-meta.py` (00:30)
  · `collect-parentlaw.sh` (00:10) — günlük veri boruları
- `aso_research.py` (Pzt 03:40) → `intel/{app}.json` — rakip ASO + kendi sıralamamız
- `brain.py` — yorum katmanı (ops host, abonelik token'lı `claude -p`)
- `setup-google.py` — Google kurulum otomasyonu (API enable + IAM + GA4→BQ link)
- `panel/` + `web.py` — kokpit (statik üretici + yazma ucu, systemd `factory-web`)
- `backup-parentlaw.sh` (01:30) — PG dump → R2, 14 gün saklama
- `schema.sql` — factory şeması (bir kez: `psql "$FACTORY_DATABASE_URL" -f schema.sql`)

Yol çözümü: künyeler `FACTORY_APPS_DIR` env'i ya da `./apps`;
intel çıktısı `FACTORY_INTEL_DIR` ya da `./intel` (gates.py tek kaynak).

## Sunucular

- **Droplet 157.230.191.204** — factory PG + Caddy (fabrika.rixe.ai) + `factory-web`
  + toplayıcı cron'ları. Env: `/root/.factory.env`, `/root/.meta.env`, `/root/.iconscout.env`.
- **Ops host 164.90.179.52** — brain.py cron'u (Claude abonelik token'ı burada).

Sır yok bu repoda — hepsi sunucu env dosyalarında.
