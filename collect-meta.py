#!/usr/bin/env python3
"""Meta reklam toplayıcısı — harcama/gösterim/tık verisini factory DB'ye yazar.

Fabrikanın SON veri borusu: kota motorunun LTV:CAC hesabı ve bütçe tavanı
denetimi `ad_spend_usd` metriğine dayanır; kaynağı burasıdır.

Kampanya yokken de koşar ve günü 0 harcamayla kapatır — boru CANLI görünür,
"veri yok" ile "harcama yok" ayrımı panelde net kalır.

Env (/root/.meta.env): META_ACCESS_TOKEN, META_AD_ACCOUNT
Cron: 00:30 UTC (dünü toplar). Backfill: COLLECT_DAY=YYYY-MM-DD
"""
import json, os, subprocess, sys, urllib.parse, urllib.request
from datetime import date, timedelta

GRAPH = "https://graph.facebook.com/v21.0"


def load_env(path):
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v)


load_env("/root/.meta.env")
load_env("/root/.factory.env")

TOKEN = os.environ.get("META_ACCESS_TOKEN", "")
ACCOUNT = os.environ.get("META_AD_ACCOUNT", "")
APP = os.environ.get("FACTORY_APP_ID", "parentlaw")
DAY = os.environ.get("COLLECT_DAY") or (date.today() - timedelta(days=1)).isoformat()

if not TOKEN or not ACCOUNT:
    print("[meta] META_ACCESS_TOKEN / META_AD_ACCOUNT tanımlı değil", file=sys.stderr)
    sys.exit(1)


def graph(path, **params):
    params["access_token"] = TOKEN
    url = f"{GRAPH}/{path}?" + urllib.parse.urlencode(params)
    try:
        return json.load(urllib.request.urlopen(url, timeout=45))
    except urllib.error.HTTPError as e:
        return {"_err": e.code, "_body": e.read()[:300].decode(errors="replace")}


# Günlük hesap özeti + kampanya kırılımı tek insights çağrısında.
ins = graph(f"{ACCOUNT}/insights",
            level="campaign",
            fields="campaign_name,spend,impressions,clicks,ctr,cpc",
            time_range=json.dumps({"since": DAY, "until": DAY}))
if "_err" in ins:
    print(f"[meta] insights hatası: {ins['_err']} {ins['_body'][:160]}", file=sys.stderr)
    sys.exit(1)

rows = ins.get("data", [])
total_spend = sum(float(r.get("spend", 0)) for r in rows)
total_imp = sum(int(r.get("impressions", 0)) for r in rows)
total_clicks = sum(int(r.get("clicks", 0)) for r in rows)

metrics = [
    (DAY, "ad_spend_usd", "", round(total_spend, 2)),
    (DAY, "ad_impressions", "", total_imp),
    (DAY, "ad_clicks", "", total_clicks),
]
for r in rows:
    seg = "campaign=" + (r.get("campaign_name", "?")[:50].replace("'", ""))
    metrics.append((DAY, "ad_spend_usd", seg, round(float(r.get("spend", 0)), 2)))

sql = ["BEGIN;"]
for d_, m, seg, v in metrics:
    sql.append(
        "INSERT INTO factory.app_metrics_daily (app_id,date,metric,segment,value) "
        f"VALUES ('{APP}','{d_}','{m}','{seg}',{v}) "
        "ON CONFLICT (app_id,date,metric,segment) DO UPDATE SET "
        "value=EXCLUDED.value, collected_at=now();")
sql.append("COMMIT;")
p = subprocess.run(["psql", "-v", "ON_ERROR_STOP=1",
                    os.environ["FACTORY_DATABASE_URL"], "-q"],
                   input="\n".join(sql), capture_output=True, text=True)
if p.returncode:
    print("[meta] DB hatası:", p.stderr[:200], file=sys.stderr)
    sys.exit(1)

print(f"[meta] {DAY}: harcama ${total_spend:.2f}, {total_imp} gösterim, "
      f"{total_clicks} tık ({len(rows)} kampanya)")
