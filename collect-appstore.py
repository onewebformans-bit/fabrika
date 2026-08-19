#!/usr/bin/env python3
"""App Store toplayıcı — ASC API'den mağaza durumunu factory DB'ye yazar.

Ops host'ta veya API droplet'inde cron'la koşar (günlük, 00:15 UTC).
Yetki notu: App Manager rollü anahtarla erişilebilenler toplanır —
sürüm/build durumu, yorum sayısı ve ortalama puan, yorum metinleri (ASO
madenciliği için). İndirme/gelir raporları Admin rolü VEYA vendor number
ister; o gelince `sales` bölümü eklenecek (bkz. README).

Env (/root/.asc.env):
  ASC_KEY_PATH=/root/.asc/AuthKey_XXXX.p8
  ASC_KEY_ID=...
  ASC_ISSUER_ID=...
  ASC_APP_ID=6785801896
  FACTORY_DATABASE_URL=postgres://...
"""
import base64, json, os, subprocess, sys, time, urllib.request, urllib.error
from datetime import date

def load_env(path="/root/.asc.env"):
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)

load_env()
KEY = os.environ["ASC_KEY_PATH"]; KID = os.environ["ASC_KEY_ID"]
ISS = os.environ["ASC_ISSUER_ID"]; APP = os.environ["ASC_APP_ID"]
APP_SLUG = os.environ.get("FACTORY_APP_ID", "parentlaw")


def _b64(d): return base64.urlsafe_b64encode(d).rstrip(b'=')


def _raw(der):
    """DER ECDSA imzasını JWS'in beklediği r||s biçimine çevirir."""
    i = 2
    assert der[i] == 2; l = der[i + 1]; r = der[i + 2:i + 2 + l]; i = i + 2 + l
    assert der[i] == 2; l2 = der[i + 1]; s = der[i + 2:i + 2 + l2]
    return r.lstrip(b'\0').rjust(32, b'\0') + s.lstrip(b'\0').rjust(32, b'\0')


def token():
    h = _b64(json.dumps({"alg": "ES256", "kid": KID, "typ": "JWT"}).encode())
    p = _b64(json.dumps({"iss": ISS, "iat": int(time.time()),
                         "exp": int(time.time()) + 900,
                         "aud": "appstoreconnect-v1"}).encode())
    sg = h + b'.' + p
    tmp = "/tmp/_asc_sign"
    open(tmp, 'wb').write(sg)
    der = subprocess.run(['openssl', 'dgst', '-sha256', '-sign', KEY, tmp],
                         capture_output=True).stdout
    return (sg + b'.' + _b64(_raw(der))).decode()


T = token()


def api(path):
    req = urllib.request.Request("https://api.appstoreconnect.apple.com" + path,
                                 headers={"Authorization": f"Bearer {T}"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        return {"_err": e.code, "_body": e.read()[:200].decode(errors='replace')}


metrics = []   # (metric, segment, value)
today = date.today().isoformat()

# --- yorumlar: puan ortalaması + sayı + son yorumların duygu dökümü ------------
ratings = {}
total = 0
cursor = "/v1/apps/%s/customerReviews?limit=200&sort=-createdDate" % APP
pages = 0
recent = []
while cursor and pages < 5:
    r = api(cursor)
    if '_err' in r:
        print("[appstore] yorum çekilemedi:", r, file=sys.stderr)
        break
    for d in r.get('data', []):
        a = d['attributes']
        rating = a.get('rating')
        ratings[rating] = ratings.get(rating, 0) + 1
        total += 1
        if len(recent) < 50:
            recent.append({"rating": rating, "title": a.get('title'),
                           "body": (a.get('body') or '')[:400],
                           "date": a.get('createdDate', '')[:10],
                           "territory": a.get('territory')})
    nxt = r.get('links', {}).get('next')
    cursor = nxt.replace("https://api.appstoreconnect.apple.com", "") if nxt else None
    pages += 1

if total:
    avg = sum(k * v for k, v in ratings.items()) / total
    metrics.append(("app_rating_avg", "ios", round(avg, 2)))
    metrics.append(("app_rating_count", "ios", total))
    for star, n in sorted(ratings.items()):
        metrics.append(("app_rating_hist", f"ios,star={star}", n))

# --- satış raporu: indirme + gelir -------------------------------------------
# Rapor günü, çalıştığı günün değil DÜNÜN verisidir; metrikler o güne yazılır.
# 404 = "o gün satış yok" (yetki hatası değil) → sessizce sıfır geçilir.
VENDOR = os.environ.get("ASC_VENDOR_NUMBER")
SALES_DAY = os.environ.get("SALES_DAY")  # elle backfill için YYYY-MM-DD


def sales_for(day):
    import gzip, io
    url = ("https://api.appstoreconnect.apple.com/v1/salesReports"
           f"?filter[frequency]=DAILY&filter[reportSubType]=SUMMARY"
           f"&filter[reportType]=SALES&filter[vendorNumber]={VENDOR}"
           f"&filter[reportDate]={day}&filter[version]=1_0")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {T}", "Accept": "application/a-gzip"})
    try:
        raw = urllib.request.urlopen(req, timeout=45).read()
        return gzip.GzipFile(fileobj=io.BytesIO(raw)).read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return ""      # o gün satış yok
        print(f"[appstore] sales {day}: HTTP {e.code}", file=sys.stderr)
        return None


sales_metrics = []
if VENDOR:
    from datetime import timedelta
    day = SALES_DAY or (date.today() - timedelta(days=1)).isoformat()
    txt = sales_for(day)
    if txt is not None:
        units_total = 0.0
        proceeds = 0.0
        by_country = {}
        for line in [l for l in txt.strip().split('\n') if l][1:]:
            f = dict(zip(txt.split('\n')[0].split('\t'), line.split('\t')))
            try:
                u = int(f.get('Units') or 0)
            except ValueError:
                u = 0
            # 1/1F/1T = ilk indirme, 7/7F = güncelleme (indirme sayılmaz)
            ptype = (f.get('Product Type Identifier') or '').strip()
            if ptype.startswith('1'):
                units_total += u
                cc = (f.get('Country Code') or '??').strip()
                by_country[cc] = by_country.get(cc, 0) + u
            try:
                proceeds += float(f.get('Developer Proceeds') or 0) * u
            except ValueError:
                pass
        sales_metrics.append((day, "store_downloads", "ios", units_total))
        sales_metrics.append((day, "store_proceeds_usd", "ios", round(proceeds, 2)))
        for cc, u in sorted(by_country.items(), key=lambda x: -x[1])[:10]:
            sales_metrics.append((day, "store_downloads", f"ios,country={cc}", u))

# --- sürüm durumu -------------------------------------------------------------
vs = api(f"/v1/apps/{APP}/appStoreVersions?limit=5")
live = review = None
for d in vs.get('data', []):
    a = d['attributes']
    st = a.get('appStoreState')
    if st == 'READY_FOR_SALE' and not live:
        live = a.get('versionString')
    if st in ('WAITING_FOR_REVIEW', 'IN_REVIEW', 'PENDING_DEVELOPER_RELEASE') and not review:
        review = f"{a.get('versionString')}:{st}"
metrics.append(("app_version_live", f"ios,v={live or 'yok'}", 1))
if review:
    metrics.append(("app_version_in_review", f"ios,{review}", 1))

# --- factory DB'ye yaz --------------------------------------------------------
if not metrics and not sales_metrics:
    print("[appstore] yazılacak metrik yok"); sys.exit(0)

# (tarih, metrik, segment, değer) — durum metrikleri bugüne, satış kendi gününe
rowset = [(today, m, s, v) for m, s, v in metrics] + list(sales_metrics)

sql = ["BEGIN;"]
for d_, m, seg, v in rowset:
    sql.append(
        "INSERT INTO factory.app_metrics_daily (app_id, date, metric, segment, value) "
        f"VALUES ('{APP_SLUG}', '{d_}', '{m}', '{seg}', {v}) "
        "ON CONFLICT (app_id, date, metric, segment) "
        "DO UPDATE SET value = EXCLUDED.value, collected_at = now();")
sql.append("COMMIT;")
p = subprocess.run(["psql", os.environ["FACTORY_DATABASE_URL"], "-q"],
                   input="\n".join(sql), capture_output=True, text=True)
if p.returncode:
    print("[appstore] DB hatası:", p.stderr[:300], file=sys.stderr); sys.exit(1)

# yorumları ayrıca diske bırak — ASO/ürün madenciliği beyin tarafında okunacak
out = os.environ.get("REVIEWS_OUT", "/var/lib/factory/reviews-ios.json")
os.makedirs(os.path.dirname(out), exist_ok=True)
json.dump(recent, open(out, "w"), ensure_ascii=False, indent=1)
print(f"[appstore] {len(rowset)} metrik yazıldı "
      f"({len(sales_metrics)} satış), {len(recent)} yorum kaydedildi")
