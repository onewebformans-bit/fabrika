#!/usr/bin/env bash
# ParentLaw gece yedeği — API droplet'inde çalışır (cron 01:30 UTC).
# pg_dump (docker) + storage arşivi → Cloudflare R2 (offsite). 14 gün saklanır.
# Mahkeme-kanıt ürününde tek-sunucu veri = kabul edilemez; bu script o riski kapatır.
set -euo pipefail
source /root/.backup.env   # R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ACCOUNT_ID, R2_BUCKET_NAME

DAY=$(date -u +%F)
APP_DIR=/var/www/coparent-api
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
export AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
EP="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
DEST="s3://${R2_BUCKET_NAME}/parentlaw-backups"

cd "$APP_DIR"
docker compose exec -T db pg_dump -U coparent -d coparent --format=custom > "$TMP/db-$DAY.dump"
tar -czf "$TMP/storage-$DAY.tar.gz" -C "$APP_DIR" storage

aws s3 cp --endpoint-url "$EP" --only-show-errors "$TMP/db-$DAY.dump" "$DEST/db-$DAY.dump"
aws s3 cp --endpoint-url "$EP" --only-show-errors "$TMP/storage-$DAY.tar.gz" "$DEST/storage-$DAY.tar.gz"

# 14 günden eskiyi temizle (dosya adındaki tarihe göre — basit ve deterministik)
CUTOFF=$(date -u -d '14 days ago' +%F)
aws s3 ls --endpoint-url "$EP" "$DEST/" | awk '{print $4}' | while read -r f; do
  [ -z "$f" ] && continue
  d=$(echo "$f" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}' | head -1)
  if [ -n "$d" ] && [[ "$d" < "$CUTOFF" ]]; then
    aws s3 rm --endpoint-url "$EP" --only-show-errors "$DEST/$f"
  fi
done

echo "[backup] $DAY tamam: $(du -sh "$TMP" | cut -f1) yüklendi"
