#!/usr/bin/env bash
# ParentLaw günlük toplayıcı — PARENTLAW SUNUCUSUNDA çalışır (89.252.142.130).
# Yerel docker PG'den dünün metriklerini okur, factory DB'ye (DO Managed PG) yazar.
#
# Kurulum (parentlaw sunucusunda):
#   1) /root/.factory.env dosyası: FACTORY_DATABASE_URL=postgres://...  (TLS'li DO URL)
#   2) apt-get install -y postgresql-client   (psql, factory'ye yazmak için)
#   3) crontab: 10 0 * * * /var/www/coparent-api/factory/collect-parentlaw.sh >> /var/log/factory-collect.log 2>&1
#
# Idempotent: aynı gün için tekrar koşarsa üzerine yazar (ON CONFLICT UPDATE).

set -euo pipefail
source /root/.factory.env

APP_DIR="${APP_DIR:-/var/www/coparent-api}"
DAY="${1:-$(date -u -d 'yesterday' +%F)}"   # istenirse elle: ./collect-parentlaw.sh 2026-08-17

# Yerel coparent DB'den tek sorguda metric,segment,value satırları üret.
# docker compose servisi 'db', kullanıcı/db 'coparent' (docker-compose.yml).
rows=$(cd "$APP_DIR" && docker compose exec -T db psql -U coparent -d coparent -At -F'|' <<SQL
WITH day AS (SELECT '$DAY'::date AS d)
SELECT 'users_total','',count(*) FROM users
UNION ALL
SELECT 'users_new','',count(*) FROM users, day WHERE created_at::date = d
UNION ALL
SELECT 'households_total','',count(*) FROM households
UNION ALL
-- anlık abonelik fotoğrafı: plan+durum kırılımı (expired hariç)
SELECT 'subs_snapshot','plan='||plan||',status='||status,count(*)
  FROM subscriptions WHERE status <> 'expired' GROUP BY plan, status
UNION ALL
-- o gün başlayan trial'lar
SELECT 'trial_starts','',count(*) FROM subscriptions, day
  WHERE created_at::date = d AND status = 'trialing'
UNION ALL
-- o gün açılan abonelikler (trial dahil)
SELECT 'subs_new','',count(*) FROM subscriptions, day WHERE created_at::date = d
UNION ALL
SELECT 'court_pack_total','',count(*) FROM purchases WHERE product = 'court_record_pack';
SQL
)

[ -z "$rows" ] && { echo "[factory] $DAY: sorgu boş döndü, yazılmadı"; exit 1; }

# Factory DB'ye yaz.
{
  echo "BEGIN;"
  while IFS='|' read -r metric segment value; do
    [ -z "$metric" ] && continue
    printf "INSERT INTO factory.app_metrics_daily (app_id, date, metric, segment, value)
            VALUES ('parentlaw', '%s', '%s', '%s', %s)
            ON CONFLICT (app_id, date, metric, segment)
            DO UPDATE SET value = EXCLUDED.value, collected_at = now();\n" \
      "$DAY" "$metric" "$segment" "$value"
  done <<< "$rows"
  echo "COMMIT;"
} | psql "$FACTORY_DATABASE_URL" -q

echo "[factory] $DAY: $(wc -l <<< "$rows") metrik yazıldı"
