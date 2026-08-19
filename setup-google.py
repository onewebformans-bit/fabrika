#!/usr/bin/env python3
"""Google tarafı kurulumunu OTOMATİK yapar — yeni uygulamada tıklama kalmasın.

Dört adım, hepsi API ile:
  1. Gerekli API'leri aç        (Service Usage API)
  2. Veri servis hesabına BigQuery rollerini ver   (Cloud Resource Manager)
  3. GA4 property'sine BigQuery bağlantısı kur     (Analytics Admin API)
  4. Sonucu doğrula                                (pipeline.check_measure)

YETKİ: Bu iş, uygulamaların günlük veri hesabıyla YAPILAMAZ; proje üzerinde
yönetici yetkisi ister. Fabrika için BİR KEZ bir "kurulum servis hesabı"
açılır, ondan sonra her yeni uygulama tek komutla kurulur.

Kurulum hesabına verilecek roller (proje düzeyinde):
  · roles/serviceusage.serviceUsageAdmin   (API açma)
  · roles/resourcemanager.projectIamAdmin  (rol verme)
  · GA4 property'sinde "Administrator" (Analytics Admin API için)

Kullanım:
    FACTORY_ADMIN_SA=/yol/admin-sa.json python3 setup-google.py check   parentlaw
    FACTORY_ADMIN_SA=/yol/admin-sa.json python3 setup-google.py apply   parentlaw
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _google import gapi, SCOPE_BQ  # noqa: E402
from gates import yaml_lite, APPS_DIR, load_env  # noqa: E402

SCOPE_CLOUD = "https://www.googleapis.com/auth/cloud-platform"
SCOPE_GA_EDIT = "https://www.googleapis.com/auth/analytics.edit"

# Fabrikanın ihtiyaç duyduğu API'ler — her uygulamada aynı.
NEEDED_APIS = [
    "playdeveloperreporting.googleapis.com",   # Android çökme/kurulum
    "bigquery.googleapis.com",                 # funnel sorguları
    "analyticsadmin.googleapis.com",           # GA4 → BigQuery bağlantısı
    "androidpublisher.googleapis.com",         # Play yorumları/faturalama
]

# Veri hesabına verilecek roller — okuma + sorgu çalıştırma.
DATA_ROLES = ["roles/bigquery.dataViewer", "roles/bigquery.jobUser"]


def admin_sa():
    sa = os.environ.get("FACTORY_ADMIN_SA", "")
    if not sa or not os.path.exists(sa):
        raise SystemExit(
            "FACTORY_ADMIN_SA tanımlı değil.\n"
            "Bir kereye mahsus kurulum hesabı gerekiyor:\n"
            "  console.cloud.google.com → IAM & Admin → Service Accounts → Create\n"
            "  roller: Service Usage Admin + Project IAM Admin\n"
            "  anahtar (JSON) indir, yolunu FACTORY_ADMIN_SA olarak ver.\n"
            "  Ayrıca GA4 → Admin → Property access → o hesabı 'Administrator' ekle.")
    return sa


def cfg_for(app):
    return yaml_lite(os.path.join(APPS_DIR, app + ".yaml"))


def api_durumu(sa, proj_num):
    """Hangi API'ler açık?"""
    out = {}
    for api in NEEDED_APIS:
        r = gapi(sa, SCOPE_CLOUD,
                 f"https://serviceusage.googleapis.com/v1/projects/{proj_num}/services/{api}")
        out[api] = r.get("state") if "_err" not in r else f"?{r['_err']}"
    return out


def api_ac(sa, proj_num, api):
    return gapi(sa, SCOPE_CLOUD,
                f"https://serviceusage.googleapis.com/v1/projects/{proj_num}/services/{api}:enable",
                body={})


def iam_ver(sa, proj, member, roles):
    """Veri hesabına rolleri ekler (mevcut politikayı okuyup üstüne yazar)."""
    pol = gapi(sa, SCOPE_CLOUD,
               f"https://cloudresourcemanager.googleapis.com/v1/projects/{proj}:getIamPolicy",
               body={})
    if "_err" in pol:
        return pol
    bindings = pol.get("bindings", [])
    mem = f"serviceAccount:{member}"
    changed = False
    for role in roles:
        b = next((x for x in bindings if x.get("role") == role), None)
        if b is None:
            bindings.append({"role": role, "members": [mem]})
            changed = True
        elif mem not in b.get("members", []):
            b.setdefault("members", []).append(mem)
            changed = True
    if not changed:
        return {"_noop": True}
    pol["bindings"] = bindings
    return gapi(sa, SCOPE_CLOUD,
                f"https://cloudresourcemanager.googleapis.com/v1/projects/{proj}:setIamPolicy",
                body={"policy": pol})


def ga_property(sa, proj):
    """Firebase projesine bağlı GA4 property'sini bulur."""
    r = gapi(sa, SCOPE_GA_EDIT,
             "https://analyticsadmin.googleapis.com/v1beta/accountSummaries?pageSize=200")
    if "_err" in r:
        return None, r
    for acc in r.get("accountSummaries", []):
        for p in acc.get("propertySummaries", []):
            if proj.lower() in (p.get("displayName", "").lower()):
                return p["property"], None      # "properties/123456"
    return None, {"_err": "notfound",
                  "_body": "GA4 property bulunamadı; kurulum hesabına property "
                           "erişimi verilmemiş olabilir."}


def bq_link_durumu(sa, prop):
    r = gapi(sa, SCOPE_GA_EDIT,
             f"https://analyticsadmin.googleapis.com/v1alpha/{prop}/bigQueryLinks")
    if "_err" in r:
        return None, r
    return r.get("bigqueryLinks", []), None


def bq_link_kur(sa, prop, proj):
    return gapi(sa, SCOPE_GA_EDIT,
                f"https://analyticsadmin.googleapis.com/v1alpha/{prop}/bigQueryLinks",
                body={"project": f"projects/{proj}",
                      "dailyExportEnabled": True,
                      "streamingExportEnabled": False,
                      "includeAdvertisingId": False,
                      "exportStreams": []})


def main():
    load_env()
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    mod, app = sys.argv[1], sys.argv[2]
    cfg = cfg_for(app)
    an = cfg.get("analytics", {})
    proj = an.get("firebase_project")
    num = str(an.get("gcp_project_number", ""))
    data_sa_path = os.environ.get("FACTORY_SA") or an.get("service_account", "")
    data_sa_mail = ""
    if data_sa_path and os.path.exists(data_sa_path):
        data_sa_mail = json.load(open(data_sa_path)).get("client_email", "")
    sa = admin_sa()

    print(f"\n🔧 {cfg['name']} — Google kurulumu ({mod})")
    print(f"   proje: {proj} ({num}) · veri hesabı: {data_sa_mail or '(yok)'}\n")

    print("1) API'ler")
    durum = api_durumu(sa, num)
    for api, st in durum.items():
        işaret = "✓" if st == "ENABLED" else "○"
        print(f"   {işaret} {api}: {st}")
        if mod == "apply" and st != "ENABLED":
            r = api_ac(sa, num, api)
            print("      →", "açıldı" if "_err" not in r else f"HATA {r['_err']} {str(r['_body'])[:120]}")

    print("\n2) Veri hesabının BigQuery rolleri")
    if not data_sa_mail:
        print("   ○ veri servis hesabı künyede yok")
    elif mod == "apply":
        r = iam_ver(sa, proj, data_sa_mail, DATA_ROLES)
        print("   →", "zaten vardı" if r.get("_noop") else
              "verildi" if "_err" not in r else f"HATA {r['_err']} {str(r['_body'])[:140]}")
    else:
        print("   (apply ile verilir:", ", ".join(DATA_ROLES) + ")")

    print("\n3) GA4 → BigQuery bağlantısı")
    prop, err = ga_property(sa, proj)
    if not prop:
        print("   ○ property bulunamadı:", str(err.get("_body"))[:160])
    else:
        print("   property:", prop)
        links, err2 = bq_link_durumu(sa, prop)
        if links is None:
            print("   ○ bağlantı okunamadı:", str(err2.get("_body"))[:160])
        elif links:
            for l in links:
                print(f"   ✓ bağlı: {l.get('project')} (günlük export: "
                      f"{l.get('dailyExportEnabled')})")
        elif mod == "apply":
            r = bq_link_kur(sa, prop, proj)
            print("   →", "kuruldu" if "_err" not in r else
                  f"HATA {r['_err']} {str(r['_body'])[:160]}")
        else:
            print("   ○ bağlantı yok (apply ile kurulur)")

    print("\n4) Doğrulama")
    os.environ.setdefault("FACTORY_SA", data_sa_path)
    import pipeline
    ok, note = pipeline.check_measure(cfg)
    print(("   ✓ " if ok else "   ○ ") + note)
    print("\nNot: GA4 bağlantısı yeni kurulduysa analytics_* dataset'i ilk günlük "
          "aktarımda oluşur (~24 saat).")


if __name__ == "__main__":
    main()
