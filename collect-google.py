#!/usr/bin/env python3
"""Google tarafı toplayıcı — funnel (BigQuery), Android (Play), çökme (Reporting).

Üç kaynağı tek koşuda dener; biri kapalıysa diğerleri yazılmaya devam eder ve
kapalı olanın NASIL açılacağını loga yazar. Fabrikanın kör noktalarını kapatan
en önemli toplayıcı: kapı eşiklerinin (onboarding, paywall, retention, crash)
veri kaynağı burasıdır.

Env (/root/.google.env):
  GOOGLE_SA_JSON=/etc/goparent/play-service-account.json
  GCP_PROJECT=coparent-9ca78
  PLAY_PACKAGE=com.thinkedgeapps.parentLaw
  FACTORY_APP_ID=parentlaw
  FACTORY_DATABASE_URL=postgres://...
"""
import json, os, subprocess, sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _google import gapi, SCOPE_BQ, SCOPE_PLAY, SCOPE_REPORT  # noqa: E402


def load_env(path):
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v)


load_env("/root/.google.env")
load_env("/root/.factory.env")

SA = os.environ.get("GOOGLE_SA_JSON", "")
PROJECT = os.environ.get("GCP_PROJECT", "")
PKG = os.environ.get("PLAY_PACKAGE", "")
APP = os.environ.get("FACTORY_APP_ID", "parentlaw")
DAY = os.environ.get("COLLECT_DAY") or (date.today() - timedelta(days=1)).isoformat()

metrics = []      # (tarih, metrik, segment, değer)
notes = []


# --- 1) BigQuery: davranış funnel'ı -------------------------------------------
# GA4 export tablosu events_YYYYMMDD. Kapı eşiklerinin dördü buradan gelir:
# onboarding tamamlama, paywall görüntüleme, trial→paid, D1 retention.
def bigquery_funnel():
    # Dataset listesini datasets.list ile DEĞİL, INFORMATION_SCHEMA ile al:
    # datasets.list ayrı bir yetki ister ve o yetki olmadan BOŞ liste döner —
    # yani "export yok" gibi görünür. Sorgu yolu bizde çalışan yol.
    def bq(sql):
        return gapi(SA, SCOPE_BQ,
                    f"https://bigquery.googleapis.com/bigquery/v2/projects/{PROJECT}/queries",
                    body={"query": sql, "useLegacySql": False, "timeoutMs": 45000})

    sch = bq(f"SELECT schema_name FROM `{PROJECT}`.INFORMATION_SCHEMA.SCHEMATA")
    if "_err" in sch:
        if "bigquery.jobs.create" in str(sch.get("_body", "")):
            notes.append("BigQuery IAM YETKİSİ YOK → console.cloud.google.com → IAM & Admin → "
                         "servis hesabına 'BigQuery Data Viewer' + 'BigQuery Job User' ver.")
        else:
            notes.append(f"BigQuery erişilemedi: {sch['_err']} {str(sch.get('_body',''))[:120]}")
        return
    names = [r["f"][0]["v"] for r in sch.get("rows", [])]
    ga4 = next((n for n in names if n.startswith("analytics_")), None)
    if not ga4:
        notes.append("BigQuery bağlı ✅ (dataset: " + (", ".join(names) or "yok") + ") ama "
                     "GOOGLE ANALYTICS export'u seçili değil → Firebase → Project settings → "
                     "Integrations → BigQuery → Manage → 'Google Analytics' ürününü aç. "
                     "Funnel (onboarding/paywall/retention) yalnız analytics_* dataset'inden gelir.")
        return

    d = DAY.replace("-", "")
    sql = f"""
    WITH e AS (
      SELECT user_pseudo_id, event_name, event_date
      FROM `{PROJECT}.{ga4}.events_{d}`
    )
    SELECT event_name, COUNT(*) AS n, COUNT(DISTINCT user_pseudo_id) AS users
    FROM e
    WHERE event_name IN ('first_open','onboarding_started','onboarding_completed',
                         'paywall_view','trial_start','trial_converted','purchase_completed')
    GROUP BY event_name
    """
    res = gapi(SA, SCOPE_BQ,
               f"https://bigquery.googleapis.com/bigquery/v2/projects/{PROJECT}/queries",
               body={"query": sql, "useLegacySql": False, "timeoutMs": 50000})
    if "_err" in res:
        notes.append(f"BigQuery sorgusu başarısız: {res['_err']} {res.get('_body','')[:120]}")
        return
    counts = {}
    for row in res.get("rows", []):
        f = row["f"]
        counts[f[0]["v"]] = {"n": int(f[1]["v"]), "users": int(f[2]["v"])}
    for name, c in counts.items():
        metrics.append((DAY, f"ev_{name}", "", c["users"]))

    # oranlar: kapı motoru bunları okur
    opened = counts.get("onboarding_started", {}).get("users", 0)
    done = counts.get("onboarding_completed", {}).get("users", 0)
    pw = counts.get("paywall_view", {}).get("users", 0)
    installs = counts.get("first_open", {}).get("users", 0)
    if opened:
        metrics.append((DAY, "onboarding_completion", "", round(done / opened, 4)))
    if installs:
        metrics.append((DAY, "paywall_view_rate", "", round(pw / installs, 4)))
    notes.append(f"BigQuery funnel: {len(counts)} event tipi ({ga4})")


# --- 2) Play: Android yorumları -----------------------------------------------
def play_reviews():
    r = gapi(SA, SCOPE_PLAY,
             f"https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{PKG}/reviews")
    if "_err" in r:
        notes.append(f"Play yorumları alınamadı: {r['_err']}")
        return
    revs = r.get("reviews", [])
    stars = [c["userComment"]["starRating"]
             for rv in revs for c in rv.get("comments", []) if "userComment" in c]
    metrics.append((date.today().isoformat(), "app_rating_count", "android", len(stars)))
    if stars:
        metrics.append((date.today().isoformat(), "app_rating_avg", "android",
                        round(sum(stars) / len(stars), 2)))
    notes.append(f"Play yorumları: {len(stars)}")


# --- 3) Play Reporting: install + çökme oranı ---------------------------------
def play_reporting():
    """Çökme oranı. Tarihi TAHMİN ETME: Play'in veri tazeliği bölgeye göre
    1-2 gün geride ve kendi saat diliminde; önce metrik setine 'en son ne
    zamana kadar verin var' diye sorup o aralığı kullanıyoruz."""
    base = f"https://playdeveloperreporting.googleapis.com/v1beta1/apps/{PKG}"
    meta = gapi(SA, SCOPE_REPORT, f"{base}/crashRateMetricSet")
    if "_err" in meta:
        if meta["_err"] == 403 and "has not been used" in str(meta.get("_body", "")):
            notes.append("Play Developer Reporting API KAPALI → console.cloud.google.com → "
                         f"proje {PROJECT} → APIs & Services → "
                         "'Google Play Developer Reporting API' → Enable")
        else:
            notes.append(f"Play Reporting erişilemedi: {meta['_err']} (503 geçicidir)")
        return

    fresh = next((f for f in meta.get("freshnessInfo", {}).get("freshnesses", [])
                  if f.get("aggregationPeriod") == "DAILY"), None)
    if not fresh:
        notes.append("Play Reporting: günlük veri henüz yok (yeni uygulama)")
        return
    t = fresh["latestEndTime"]
    tz = t.get("timeZone", {"id": "America/Los_Angeles"})
    # Aralık kuralı (deneyerek bulundu): başlangıç, tazelik gününden ÖNCE
    # olmalı; bitiş tazelik gününe eşit olabilir. Yani [tazelik-1, tazelik].
    end = date(t["year"], t["month"], t["day"])
    start = end - timedelta(days=1)
    day_str = start.isoformat()
    pt = lambda d: {"year": d.year, "month": d.month, "day": d.day, "timeZone": tz}

    q = gapi(SA, SCOPE_REPORT, f"{base}/crashRateMetricSet:query",
             body={"timelineSpec": {"aggregationPeriod": "DAILY",
                                    "startTime": pt(start), "endTime": pt(end)},
                   "metrics": ["crashRate", "distinctUsers"]})
    if "_err" in q:
        notes.append(f"Play Reporting sorgusu: {q['_err']} {str(q.get('_body',''))[:100]}")
        return
    rows = q.get("rows", [])
    if not rows:
        notes.append(f"Play Reporting bağlı ✅ ama {day_str} için satır yok "
                     "(Android kurulumu henüz yok)")
        return
    for row in rows:
        vals = {m["metric"]: m.get("decimalValue", {}).get("value")
                for m in row.get("metrics", [])}
        if vals.get("crashRate") is not None:
            metrics.append((day_str, "crash_free_rate", "android",
                            round(1 - float(vals["crashRate"]), 4)))
        if vals.get("distinctUsers") is not None:
            metrics.append((day_str, "android_active_users", "android",
                            float(vals["distinctUsers"])))
    notes.append(f"Play Reporting: {day_str} verisi alındı")


# --- 4) Play track'leri: hangi versionCode nerede -----------------------------
def play_tracks():
    """production/internal'daki versionCode'u metriğe yaz — panel/beyin Android
    sürüm cephesini görsün. NOT: track 'completed' = istenen durum; Google
    incelemesinin bitip bitmediğini API söylemez."""
    ed = gapi(SA, SCOPE_PLAY,
              f"https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{PKG}/edits",
              method="POST")
    if "_err" in ed:
        notes.append(f"Play tracks: edit açılamadı {ed['_err']}")
        return
    tr = gapi(SA, SCOPE_PLAY,
              f"https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{PKG}"
              f"/edits/{ed['id']}/tracks")
    if "_err" in tr:
        notes.append(f"Play tracks alınamadı: {tr['_err']}")
        return
    today = date.today().isoformat()
    for t in tr.get("tracks", []):
        for rel in t.get("releases", []):
            if rel.get("status") == "completed" and rel.get("versionCodes"):
                metrics.append((today, "play_track_version", t["track"],
                                int(rel["versionCodes"][0])))
    notes.append("Play tracks okundu")


for fn in (bigquery_funnel, play_reviews, play_reporting, play_tracks):
    try:
        fn()
    except Exception as e:
        notes.append(f"{fn.__name__} hata: {e}")

if metrics and os.environ.get("FACTORY_DATABASE_URL"):
    sql = ["BEGIN;"]
    for d_, m, seg, v in metrics:
        sql.append(
            "INSERT INTO factory.app_metrics_daily (app_id,date,metric,segment,value) "
            f"VALUES ('{APP}','{d_}','{m}','{seg}',{v}) "
            "ON CONFLICT (app_id,date,metric,segment) DO UPDATE SET "
            "value=EXCLUDED.value, collected_at=now();")
    sql.append("COMMIT;")
    p = subprocess.run(["psql", os.environ["FACTORY_DATABASE_URL"], "-q"],
                       input="\n".join(sql), capture_output=True, text=True)
    if p.returncode:
        print("[google] DB hatası:", p.stderr[:200], file=sys.stderr)

print(f"[google] {len(metrics)} metrik yazıldı")
for n in notes:
    print("  ·", n)
