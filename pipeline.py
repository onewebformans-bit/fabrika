#!/usr/bin/env python3
"""Fabrika boru hattı — fikirden ölçeğe, sırayla.

Bir uygulamanın hangi adımda olduğunu ölçer, sıradaki adımı söyler, mümkün
olanı çalıştırır. Bugün ParentLaw için ELLE yapılan her adım burada bir
aşamadır; yeni uygulama künyesini doldurunca aynı sıra kendiliğinden işler.

    python3 pipeline.py status <app>     # nerede kaldık
    python3 pipeline.py run <app> <stage>

Aşamalar (sıra bağlayıcı — önceki tamamlanmadan sonraki çalışmaz):

  1 instrument  Analitik etiketleri + event sözleşmesi kodda mı        [otomatik denetim]
  2 backend     API + faturalama + push sırları yerinde mi             [otomatik denetim]
  3 signing     iOS sertifika/profil + Android keystore hazır mı       [otomatik üretim]
  4 ship        İmzalı build üret + mağazaya yükle                     [otomatik]
  5 intel       Rakip ASO + reklam istihbaratı                         [otomatik]
  6 aso         Kendi metadata paketimiz                               [otomatik üretim]
  7 creative    Mağaza görselleri + reklam kreatifleri                 [yarı-otomatik]
  8 ads         Kampanya kurulumu ve günlük yönetim                    [onaylı]
  9 watch       Tek kanalda ölçüm + kapı/kota kararları                [sürekli]

Aşama durumu koda değil GERÇEĞE bakarak ölçülür (dosya var mı, API ne diyor,
DB'de veri var mı) — bir bayrak dosyası "tamam" derken gerçek "eksik" olamaz.
"""
import json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from gates import yaml_lite, load_env, q  # noqa: E402

from gates import APPS_DIR  # tek kaynak: gates.py yolu çözer

STAGES = ["instrument", "measure", "backend", "signing", "ship", "intel", "aso",
          "creative", "ads", "watch"]


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                          text=True, **kw)


# --- aşama denetçileri: her biri (tamam_mı, açıklama) döndürür ----------------

def check_instrument(cfg):
    repo = os.path.expanduser(cfg.get("repo", ""))
    if not os.path.isdir(repo):
        return False, f"repo bulunamadı: {repo}"
    need = cfg.get("analytics", {}).get("required_events", [])
    missing = []
    for ev in need:
        r = sh(["grep", "-rq", ev, os.path.join(repo, "lib")])
        if r.returncode != 0:
            missing.append(ev)
    if missing:
        return False, f"kodda bulunamayan event: {', '.join(missing)}"
    return True, f"{len(need)} zorunlu event kodda mevcut"


def check_measure(cfg):
    """Google tarafı ölçüm altyapısı — iddiaya değil API'ye sorar.

    Her yeni uygulamada tekrar eden dört adım: Firebase projesi, BigQuery
    bağlantısı, o bağlantıda Google Analytics ürünü, Play Reporting API.
    Hangisi eksikse TAM adresiyle söyler; bu liste ParentLaw'da elle
    keşfedilen adımların kalıcı hâli.
    """
    an = cfg.get("analytics", {})
    # Servis hesabı sunucuda /etc/goparent altında, geliştirme makinesinde
    # başka yerde olabilir → env ile geçersiz kılınabilir.
    sa = os.environ.get("FACTORY_SA") or an.get("service_account", "")
    proj = an.get("firebase_project", "")
    if not proj:
        return False, "künyede analytics.firebase_project yok"
    if not sa or not os.path.exists(sa):
        return False, f"servis hesabı bulunamadı: {sa or '(künyede yok)'}"

    sys.path.insert(0, HERE)
    from _google import gapi, SCOPE_BQ, SCOPE_REPORT  # noqa: E402

    eksik = []
    # 1) BigQuery: dataset listesini INFORMATION_SCHEMA ile al (datasets.list
    #    ayrı yetki ister ve yetkisizken boş liste döner — yanıltıcı).
    r = gapi(sa, SCOPE_BQ,
             f"https://bigquery.googleapis.com/bigquery/v2/projects/{proj}/queries",
             body={"query": f"SELECT schema_name FROM `{proj}`.INFORMATION_SCHEMA.SCHEMATA",
                   "useLegacySql": False, "timeoutMs": 30000})
    if "_err" in r:
        eksik.append("BigQuery erişimi (IAM: Data Viewer + Job User)")
    else:
        names = [x["f"][0]["v"] for x in r.get("rows", [])]
        if not names:
            eksik.append("BigQuery bağlantısı (Firebase → Integrations → BigQuery)")
        elif not any(n.startswith("analytics_") for n in names):
            eksik.append("BigQuery bağlantısında Google Analytics ürünü")

    # 2) Play Reporting (Android crash/install)
    pkg = cfg.get("store", {}).get("play_package", "")
    if pkg:
        m = gapi(sa, SCOPE_REPORT,
                 f"https://playdeveloperreporting.googleapis.com/v1beta1/apps/{pkg}/crashRateMetricSet")
        if "_err" in m and m["_err"] == 403:
            eksik.append("Play Developer Reporting API (Cloud Console → Enable)")

    if eksik:
        return False, "eksik: " + "; ".join(eksik)
    return True, "BigQuery+Analytics ve Play Reporting bağlı"


def check_backend(cfg):
    api = cfg.get("backend", {}).get("api")
    if not api:
        return False, "künyede backend.api yok"
    r = sh(["curl", "-s", "-m", "10", f"{api}/health"])
    if '"ok"' not in r.stdout:
        return False, f"health yanıt vermedi: {r.stdout[:60]}"
    return True, "API ayakta (/health ok)"


def check_signing(cfg):
    repo = os.path.expanduser(cfg.get("repo", ""))
    ios_ok = bool(cfg.get("store", {}).get("ios_cert_id"))
    ks = os.path.join(repo, "android", "key.properties")
    and_ok = os.path.exists(ks)
    if ios_ok and and_ok:
        return True, "iOS sertifikası künyede, Android key.properties yerinde"
    eksik = []
    if not ios_ok: eksik.append("iOS sertifikası")
    if not and_ok: eksik.append("Android keystore")
    return False, "eksik: " + ", ".join(eksik)


def check_ship(cfg):
    """Mağazada canlı sürüm var mı — gerçeğe ASC toplayıcısının verisinden bakar."""
    rows = q(f"""SELECT segment FROM factory.app_metrics_daily
                 WHERE app_id='{cfg['id']}' AND metric='app_version_live'
                 ORDER BY date DESC LIMIT 1""")
    if rows and "v=" in rows[0][0] and "yok" not in rows[0][0]:
        return True, f"mağazada canlı: {rows[0][0].split('v=')[-1]}"
    return False, "mağazada canlı sürüm görünmüyor"


def check_intel(cfg):
    out = os.path.join(HERE, "..", "..", "factory", "intel", f"{cfg['id']}.json")
    if os.path.exists(out):
        d = json.load(open(out))
        return True, f"{len(d.get('competitors', []))} rakip çözümlendi ({d.get('generated','')[:10]})"
    return False, "rakip istihbaratı henüz üretilmedi (aso_research.py)"


def check_aso(cfg):
    repo = os.path.expanduser(cfg.get("repo", ""))
    p = os.path.join(repo, "store", "ASO.md")
    return (True, "ASO paketi hazır") if os.path.exists(p) else (False, "store/ASO.md yok")


def check_creative(cfg):
    d = os.path.join(HERE, "..", "..", "factory", "creative", cfg["id"])
    n = len(os.listdir(d)) if os.path.isdir(d) else 0
    return (n > 0, f"{n} kreatif üretilmiş" if n else "kreatif üretilmedi")


def check_ads(cfg):
    rows = q(f"""SELECT sum(value) FROM factory.app_metrics_daily
                 WHERE app_id='{cfg['id']}' AND metric='ad_spend_usd'""")
    spend = float(rows[0][0]) if rows and rows[0][0] else 0
    return (spend > 0, f"toplam reklam harcaması ${spend:.0f}" if spend
            else "reklam hesabı/kampanya yok")


def check_watch(cfg):
    rows = q(f"""SELECT count(*) FROM factory.app_metrics_daily
                 WHERE app_id='{cfg['id']}' AND date >= current_date - 2""")
    n = int(rows[0][0]) if rows else 0
    return (n > 0, f"son 2 günde {n} metrik satırı akıyor" if n
            else "toplayıcı veri yazmıyor")


CHECKS = {
    "instrument": check_instrument, "measure": check_measure, "backend": check_backend, "signing": check_signing,
    "ship": check_ship, "intel": check_intel, "aso": check_aso,
    "creative": check_creative, "ads": check_ads, "watch": check_watch,
}

# Aşamaları çalıştıran komutlar. Yalnız güvenli/otomatikleştirilebilir olanlar
# doludur; gerisi insan kapısı gerektirir ve ne yapılacağını söyler.
RUNNERS = {
    "intel": "python3 {here}/aso_research.py {app}",
    "watch": "python3 {here}/gates.py {app}",
}


def load(app):
    for ext in (".yaml", ".yml"):
        p = os.path.join(APPS_DIR, app + ext)
        if os.path.exists(p):
            return yaml_lite(p)
    raise SystemExit(f"künye bulunamadı: factory/apps/{app}.yaml")


def publish(app_id, kind, rows):
    """Modül durumunu factory DB'ye yazar — panel buradan okur.

    Kontroller nerede koşarsa koşsun (benim makinem, ops host, CI) sonuç aynı
    yerde birikir; panelin kendisi hiçbir denetim çalıştırmaz.
    """
    if not os.environ.get("FACTORY_DATABASE_URL"):
        return
    sql = ["BEGIN;", f"DELETE FROM factory.app_status WHERE app_id='{app_id}' AND kind='{kind}';"]
    for ordn, (key, ok, label, note) in enumerate(rows, 1):
        esc = lambda s: str(s).replace("'", "''")
        sql.append(
            "INSERT INTO factory.app_status (app_id,kind,key,ord,ok,label,note) VALUES "
            f"('{app_id}','{kind}','{esc(key)}',{ordn},{'true' if ok else 'false'},"
            f"'{esc(label)}','{esc(note)}');")
    sql.append("COMMIT;")
    subprocess.run(["psql", os.environ["FACTORY_DATABASE_URL"], "-q"],
                   input="\n".join(sql), capture_output=True, text=True)


def status(app):
    cfg = load(app)
    print(f"\n🏭 {cfg['name']}  [{cfg.get('status')}]\n")
    first_todo = None
    published = []
    for i, st in enumerate(STAGES, 1):
        try:
            ok, note = CHECKS[st](cfg)
        except Exception as e:
            ok, note = False, f"denetim hatası: {e}"
        published.append((st, ok, st, note))
        mark = "✓" if ok else "○"
        print(f"  {mark} {i}. {st:<11} {note}")
        if not ok and first_todo is None:
            first_todo = st
    print()
    publish(cfg["id"], "stage", published)
    if first_todo:
        runner = RUNNERS.get(first_todo)
        print(f"→ sıradaki adım: {first_todo}"
              + (f"  (python3 pipeline.py run {app} {first_todo})" if runner else "  [insan kapısı]"))
    else:
        print("→ tüm aşamalar tamam; watch döngüsü sürüyor.")
    return first_todo


def run(app, stage):
    if stage not in STAGES:
        raise SystemExit(f"bilinmeyen aşama: {stage}")
    runner = RUNNERS.get(stage)
    if not runner:
        raise SystemExit(f"'{stage}' aşaması otomatik çalıştırılamaz (insan kapısı).")
    cmd = runner.format(here=HERE, app=app)
    print("→", cmd)
    os.system(cmd)


if __name__ == "__main__":
    load_env()
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    action, app = sys.argv[1], sys.argv[2]
    if action == "status":
        status(app)
    elif action == "run":
        run(app, sys.argv[3])
    else:
        raise SystemExit(__doc__)
