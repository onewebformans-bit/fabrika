#!/usr/bin/env python3
"""Fabrika — tek giriş kapısı.

    factory add <isim>       yeni uygulamayı fabrikaya al (künye + keşif + istek listesi)
    factory needs <app>      bu uygulama için benden ne isteniyor
    factory status <app>     boru hattı nerede
    factory run <app> <asama>
    factory list             fabrikadaki uygulamalar

Tasarım: fabrikanın bildiği iki tür gereksinim var.
  * TAKIM SEVİYESİ — bir kez kurulur, tüm uygulamalar paylaşır (ASC anahtarı,
    vendor number, iOS sertifikası, Meta Business). İkinci uygulamada
    tekrar istenmez.
  * UYGULAMA SEVİYESİ — her uygulama için ayrı gerekir (Firebase projesi,
    OneSignal id, Android keystore, reklam hesabı, bütçe kararı).
`needs` yalnız EKSİK olanları ve nereden alınacağını yazar.
"""
import json, os, re, subprocess, sys, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from gates import yaml_lite, load_env  # noqa: E402
import pipeline  # noqa: E402

ROOT = os.path.join(HERE, "..", "..")
from gates import APPS_DIR  # tek kaynak: gates.py yolu çözer
ASC_ENV = "/root/.asc.env"


# --- takım seviyesi varlıklar (bir kez kurulur) -------------------------------
TEAM_ASSETS = {
    "ASC API anahtarı": lambda: bool(os.environ.get("ASC_KEY_ID")),
    "Vendor number": lambda: bool(os.environ.get("ASC_VENDOR_NUMBER")),
    "iOS dağıtım sertifikası": lambda: os.path.exists(
        os.path.expanduser("~/.parentlaw-signing/parentlaw_dist.p12")),
    "Factory DB": lambda: bool(os.environ.get("FACTORY_DATABASE_URL")),
}


# --- SABİT İNTAKE — her uygulamada AYNI soruların sorulduğu liste --------------
# Kural: fabrikanın kendi üretebildiği hiçbir şey burada yer almaz. Burada
# olanlar yalnızca insanın hesabına/kartına/koduna bağlı olanlardır.
# Sıra bağlayıcıdır: 1-3 olmadan boru hattı başlamaz, 4-5 yayın öncesi,
# 6 reklam fazında gerekir.
INTAKE = [
    {"n": 1, "key": "repo", "ask": "Kod deposunun yerel yolu",
     "why": "enstrümantasyon denetimi, imzalama ve build buradan yapılır",
     "how": "örn. ~/Projects/Roadfolk",
     "have": lambda c: bool(c.get("repo")) and os.path.isdir(os.path.expanduser(c["repo"]))},

    {"n": 2, "key": "pitch", "ask": "Uygulamanın tek cümlelik vaadi + kategorisi",
     "why": "ASO metinleri, rakip seçimi ve reklam kancaları bu cümleden türetilir",
     "how": "örn. 'yol gezisi planlayıcı — rota, mola ve bütçe tek yerde'",
     "have": lambda c: bool(c.get("market", {}).get("category"))},

    {"n": 3, "key": "pricing", "ask": "Fiyat: aylık / yıllık / kaç gün deneme",
     "why": "abonelik ürünleri, paywall ve LTV hesabı buna göre kurulur",
     "how": "örn. 8 / 79 / 7  (tam dolar önerilir, .99 yok)",
     "have": lambda c: bool(c.get("pricing", {}).get("plans"))},

    {"n": 4, "key": "firebase", "ask": "Firebase projesi (proje adı)",
     "why": "onboarding→paywall funnel'ı ve retention kohortları buradan gelir",
     "how": "console.firebase.google.com → yeni proje → iOS+Android app ekle → "
            "google-services.json ve GoogleService-Info.plist dosyalarını bana ver",
     "have": lambda c: bool(c.get("analytics", {}).get("firebase_project"))},

    {"n": 5, "key": "push", "ask": "OneSignal App ID",
     "why": "bildirim ikinci kullanıcı-edinme kanalı; trial bitimi hatırlatması buradan",
     "how": "onesignal.com → New App → App ID + REST API Key",
     "have": lambda c: bool(c.get("analytics", {}).get("push"))},

    {"n": 6, "key": "ads", "ask": "Meta reklam hesabı (ödeme yöntemi bağlı)",
     "why": "kampanya kurulumu, günlük bütçe yönetimi ve harcama verisi",
     "how": "Meta Business → bu uygulama için AYRI reklam hesabı aç, kartı bağla, "
            "hesap ID'sini ver. (Kart bağlama benim yapamayacağım tek adım.)",
     "have": lambda c: bool(c.get("ads", {}).get("meta_account_id"))},
]

# Fabrikanın senden İSTEMEDİĞİ, kendi hallettikleri — intake'i kısa tutan liste.
AUTOMATIC = [
    "App Store kaydı ve bundle id (hesapta adıyla bulunur)",
    "iOS dağıtım sertifikası + provisioning profile (takım geneli, kurulu)",
    "Android keystore (yeni uygulamada üretilir; mevcutsa bağlanır)",
    "Rakip listesi, çekirdek aramalar ve kategori sözlüğü (istihbarat modülü)",
    "ASO paketi: başlık, alt başlık, keyword, açıklama, promo metni",
    "Mağaza görselleri ve reklam kreatifleri (kreatif modülü)",
    "Bütçe/kapı eşikleri (fabrika varsayılanı; istersen künyeden değiştir)",
    "Build, imzalama, mağazaya yükleme ve sürüm takibi",
    "Günlük ölçüm, kota kararı ve otomatik durdurma önerisi",
]


def app_needs(cfg):
    """İntake'in bu uygulamada henüz verilmemiş maddeleri (sıra korunur)."""
    return [i for i in INTAKE if not i["have"](cfg)]


def asc_lookup(name):
    """Yeni uygulamayı ASC hesabında adıyla arar — id ve bundle otomatik gelsin."""
    try:
        sys.path.insert(0, HERE)
        env = {k: os.environ[k] for k in
               ("ASC_KEY_PATH", "ASC_KEY_ID", "ASC_ISSUER_ID") if k in os.environ}
        if len(env) < 3:
            return None
        code = (
            "import json,sys;sys.path.insert(0,%r);"
            "exec(open(%r).read());"
            "print(json.dumps(api('/v1/apps?limit=100')))" % (HERE, os.path.join(HERE, "_asc.py")))
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        if r.returncode:
            return None
        data = json.loads(r.stdout)
        for a in data.get("data", []):
            at = a["attributes"]
            if name.lower() in (at.get("name") or "").lower():
                return {"asc_app_id": a["id"], "bundle_id": at.get("bundleId"),
                        "store_name": at.get("name")}
    except Exception:
        return None
    return None


def guess_searches(category):
    """Kategori teriminden çekirdek arama adayları üretir (App Store önerileri)."""
    try:
        u = ("https://itunes.apple.com/search?" + urllib.parse.urlencode(
            {"term": category, "country": "us", "entity": "software", "limit": 5}))
        res = json.load(urllib.request.urlopen(u, timeout=20)).get("results", [])
        # rakiplerin başlıklarındaki tekrar eden ikilileri arama adayı say
        cand = collections_counter(res)
        return [category] + cand[:4]
    except Exception:
        return [category]


def collections_counter(res):
    import collections
    c = collections.Counter()
    for r in res:
        t = re.sub(r"[^a-zA-Z\s]", " ", r.get("trackName", "")).lower().split()
        for i in range(len(t) - 1):
            pair = f"{t[i]} {t[i+1]}"
            if len(pair) > 8:
                c[pair] += 1
    return [p for p, n in c.most_common(8) if n > 1]


TEMPLATE = """# {name} — fabrika künyesi
# `factory add` ile oluşturuldu. Boş alanları doldur; `factory needs {id}` ne
# eksik olduğunu her zaman söyler.

id: {id}
name: {name}
status: idea                      # idea|build|soak|spark|scale|harvest|killed
repo: {repo}
bundle_id: {bundle_id}

market:
  primary: US
  languages: [en-US]
  category: {category}
  competitors: []
  core_searches:
{searches}

store:
  asc_app_id: "{asc_app_id}"
  vendor_number: "{vendor}"       # takım geneli — aynı hesaptaki tüm uygulamalar
  play_package: {bundle_id}
  team_id: "{team_id}"

backend:
  api:                            # varsa kendi API'si
  revenue_source: store           # store|backend_db

analytics:
  firebase_project:
  required_events:
    - onboarding_started
    - onboarding_completed
    - paywall_view
    - trial_start
    - purchase_completed
  push:

pricing:
  model: subscription
  trial_days: 7

# --- KOTA VE KAPILAR — fabrikanın frenleri ------------------------------------
budget:
  daily_cap_usd: 50
  spark_total_cap_usd: 2500       # tavan dolar + kıvılcım yoksa → otomatik KILL önerisi
  scale_step_pct: 25

gates:
  soak:
    crash_free_rate: 0.995
    onboarding_completion: 0.60
    paywall_view_rate: 0.70
  spark:
    trial_start_rate: 0.05
    trial_to_paid: 0.35
    d1_retention: 0.25
    ltv_cac: 1.0
  scale:
    ltv_cac: 1.2
    consecutive_weeks: 2

min_sample:
  installs: 100
  trials: 20
"""


def cmd_add(name):
    app_id = re.sub(r"[^a-z0-9]+", "", name.lower())[:24] or "app"
    path = os.path.join(APPS_DIR, f"{app_id}.yaml")
    if os.path.exists(path):
        print(f"künye zaten var: {path}\n"); return cmd_needs(app_id)

    print(f"\n🏭 '{name}' fabrikaya alınıyor…\n")
    found = asc_lookup(name)
    if found:
        print(f"  ✓ App Store'da bulundu: {found['store_name']} "
              f"(id {found['asc_app_id']}, {found['bundle_id']})")
    else:
        print("  · App Store'da bulunamadı (henüz yayınlanmamış olabilir)")

    # repo tahmini: ~/Projects altında isme benzeyen klasör
    repo = ""
    proj = os.path.expanduser("~/Projects")
    if os.path.isdir(proj):
        for d in os.listdir(proj):
            if app_id in re.sub(r"[^a-z0-9]+", "", d.lower()):
                repo = os.path.join("~/Projects", d)
                print(f"  ✓ yerel repo: {repo}")
                break

    # Kategori uygulama adından TAHMİN EDİLMEZ — "roadfolk" bir kategori değil.
    # İntake'in 2. maddesi (tek cümlelik vaat) gelince doldurulur; o cümleden
    # kategori ve çekirdek aramalar türetilir.
    category, searches = "", []

    os.makedirs(APPS_DIR, exist_ok=True)
    open(path, "w").write(TEMPLATE.format(
        id=app_id, name=name, repo=repo or "",
        bundle_id=(found or {}).get("bundle_id", ""),
        asc_app_id=(found or {}).get("asc_app_id", ""),
        vendor=os.environ.get("ASC_VENDOR_NUMBER", ""),
        team_id=os.environ.get("ASC_TEAM_ID", "97SGGCW829"),
        category=category,
        searches="\n".join(f"    - {s}" for s in searches)))
    print(f"\n  ✓ künye yazıldı: apps/{app_id}.yaml")

    # DB'ye kaydet ki panel ve toplayıcılar görsün
    if os.environ.get("FACTORY_DATABASE_URL"):
        subprocess.run(["psql", os.environ["FACTORY_DATABASE_URL"], "-q", "-c",
                        f"INSERT INTO factory.apps (id,name,status) VALUES "
                        f"('{app_id}','{name}','idea') ON CONFLICT (id) DO NOTHING;"],
                       capture_output=True)
        print("  ✓ fabrika DB'sine kaydedildi (panelde görünür)")
    return cmd_needs(app_id)


def cmd_needs(app_id):
    load_env()
    load_env(ASC_ENV)
    cfg = yaml_lite(os.path.join(APPS_DIR, f"{app_id}.yaml"))

    print(f"\n📋 {cfg.get('name')} — fabrika intake formu\n")
    print("  Fabrikanın kendi hallettikleri (senden istemiyorum):")
    for a in AUTOMATIC:
        print(f"    ✓ {a}")

    todo = app_needs(cfg)
    done = [i for i in INTAKE if i not in todo]
    print(f"\n  Senden istediğim {len(INTAKE)} şey — her uygulamada aynı:\n")
    for i in INTAKE:
        mark = "✓" if i in done else " "
        print(f"  [{mark}] {i['n']}. {i['ask']}")
        if i in todo:
            print(f"        niçin: {i['why']}")
            print(f"        nasıl: {i['how']}")
    # panel bu durumu DB'den okur
    pipeline.publish(cfg["id"], "intake",
                     [(str(i["n"]), i not in todo, i["ask"],
                       "" if i not in todo else i["how"]) for i in INTAKE])

    if not todo:
        print("\n  ✅ İntake tamam — boru hattı bu uygulamayı sonuna kadar götürebilir.")
    else:
        print(f"\n  → {len(todo)} madde bekliyor; verdiğinde künyeye ben işlerim.")
    return todo


def cmd_list():
    load_env()
    print()
    for f in sorted(os.listdir(APPS_DIR)):
        if not f.endswith((".yaml", ".yml")):
            continue
        cfg = yaml_lite(os.path.join(APPS_DIR, f))
        n = len(app_needs(cfg))
        print(f"  {cfg['id']:<14} {cfg.get('status','?'):<8} "
              f"{'eksik yok' if not n else str(n) + ' eksik'}   {cfg.get('name')}")
    print()


if __name__ == "__main__":
    load_env(); load_env(ASC_ENV)
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    c = sys.argv[1]
    if c == "add":
        cmd_add(" ".join(sys.argv[2:]))
    elif c == "needs":
        cmd_needs(sys.argv[2])
    elif c == "status":
        pipeline.status(sys.argv[2])
    elif c == "run":
        pipeline.run(sys.argv[2], sys.argv[3])
    elif c == "list":
        cmd_list()
    else:
        raise SystemExit(__doc__)
