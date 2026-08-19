#!/usr/bin/env python3
"""App Store'a sürüm gönderimi — ship_play.py'nin iOS eşi. MAC'TE koşar
(Flutter derlemesi + imzalama + altool yalnız macOS'ta var).

Kullanım:
  python3 ship_ios.py <app> build                    # flutter build ipa
  python3 ship_ios.py <app> upload [ipa-yolu]        # altool ile ASC'ye yükle
  python3 ship_ios.py <app> release <1.6.0> [--notes notes.txt] [--submit]
  python3 ship_ios.py <app> status                   # sürüm/build durumu

release: işlenmiş build'i bulur, App Store sürümünü oluşturur/günceller,
build'i bağlar, notları yazar; --submit ile incelemeye gönderir.
Env: ASC_KEY_PATH, ASC_KEY_ID, ASC_ISSUER_ID (yoksa künye store.* alanları).
"""
import argparse, json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from gates import yaml_lite, APPS_DIR  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("app")
ap.add_argument("cmd", choices=["build", "upload", "release", "status"])
ap.add_argument("arg", nargs="?")           # upload: ipa yolu · release: sürüm no
ap.add_argument("--notes", help="en-US 'What's New' dosyası")
ap.add_argument("--submit", action="store_true", help="incelemeye de gönder")
a = ap.parse_args()

cfg = yaml_lite(os.path.join(APPS_DIR, a.app + ".yaml"))
st = cfg.get("store") or {}
REPO = os.path.expanduser(cfg.get("repo", ""))
APP_ID = st.get("asc_app_id")
BUNDLE = cfg.get("bundle_id")
TEAM = st.get("team_id")
PROFILE = st.get("ios_profile")

# ASC API kimliği: env öncelikli, yoksa künye
os.environ.setdefault("ASC_KEY_ID", st.get("asc_key_id", ""))
os.environ.setdefault("ASC_ISSUER_ID", st.get("asc_issuer_id", ""))
os.environ.setdefault("ASC_KEY_PATH", os.path.expanduser(st.get("asc_key_path", "")))
import _asc  # noqa: E402  (env okunduktan SONRA import — token burada üretilir)

IPA = os.path.join(REPO, "build/ios/ipa")


def run(cmd, **kw):
    print("$", " ".join(cmd))
    r = subprocess.run(cmd, **kw)
    if r.returncode:
        raise SystemExit(f"komut başarısız ({r.returncode})")


def die_if_err(r, what):
    if "_err" in r:
        raise SystemExit(f"{what}: HATA {r['_err']} {r.get('_body','')}")
    return r


if a.cmd == "build":
    # ExportOptions künyeden üretilir — repo'da elle dosya tutulmaz.
    exp = f"/tmp/ExportOptions_{a.app}.plist"
    open(exp, "w").write(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>method</key><string>app-store-connect</string>
  <key>teamID</key><string>{TEAM}</string>
  <key>signingStyle</key><string>manual</string>
  <key>provisioningProfiles</key><dict>
    <key>{BUNDLE}</key><string>{PROFILE}</string>
  </dict>
</dict></plist>""")
    run(["flutter", "build", "ipa", "--release", "--export-options-plist", exp], cwd=REPO)
    print("ipa:", subprocess.run(["ls", IPA], capture_output=True, text=True).stdout.strip())

elif a.cmd == "upload":
    ipa = a.arg or next((os.path.join(IPA, f) for f in os.listdir(IPA) if f.endswith(".ipa")), None)
    if not ipa:
        raise SystemExit("ipa bulunamadı — önce 'build'")
    # altool anahtarı ./private_keys veya ~/private_keys altında AuthKey_<ID>.p8 arar.
    run(["xcrun", "altool", "--upload-app", "-f", ipa, "-t", "ios",
         "--apiKey", os.environ["ASC_KEY_ID"], "--apiIssuer", os.environ["ASC_ISSUER_ID"]])
    print("✓ yüklendi — ASC işlemesi 5-30 dk sürer ('status' ile izle)")

elif a.cmd == "status":
    vers = die_if_err(_asc.api(
        f"/v1/apps/{APP_ID}/appStoreVersions?limit=5&fields[appStoreVersions]=versionString,appStoreState"),
        "sürümler")
    for v in vers.get("data", []):
        at = v["attributes"]
        print(f"sürüm {at['versionString']}: {at['appStoreState']}")
    blds = die_if_err(_asc.api(
        f"/v1/builds?filter[app]={APP_ID}&limit=5&sort=-uploadedDate"
        "&fields[builds]=version,processingState"), "build'ler")
    for b in blds.get("data", []):
        at = b["attributes"]
        print(f"build {at['version']}: {at['processingState']}")

elif a.cmd == "release":
    ver = a.arg
    if not ver:
        raise SystemExit("sürüm numarası gerekli, örn: release 1.6.0")

    # 1) işlenmiş en yeni build
    for _ in range(60):
        blds = die_if_err(_asc.api(
            f"/v1/builds?filter[app]={APP_ID}&limit=1&sort=-uploadedDate"
            "&fields[builds]=version,processingState"), "build'ler")
        b = (blds.get("data") or [None])[0]
        if b and b["attributes"]["processingState"] == "VALID":
            break
        print("build işleniyor, 60sn bekleniyor…",
              b["attributes"]["processingState"] if b else "yok")
        time.sleep(60)
    else:
        raise SystemExit("build VALID olmadı")
    build_id = b["id"]
    print(f"build hazır: {b['attributes']['version']}")

    # 2) sürüm var mı? yoksa oluştur
    vers = die_if_err(_asc.api(
        f"/v1/apps/{APP_ID}/appStoreVersions?filter[versionString]={ver}"), "sürüm sorgusu")
    if vers.get("data"):
        vid = vers["data"][0]["id"]
        print(f"sürüm {ver} zaten var ({vers['data'][0]['attributes']['appStoreState']})")
    else:
        v = die_if_err(_asc.api("/v1/appStoreVersions", "POST", {
            "data": {"type": "appStoreVersions",
                     "attributes": {"platform": "IOS", "versionString": ver},
                     "relationships": {"app": {"data": {"type": "apps", "id": APP_ID}}}}}),
            "sürüm oluşturma")
        vid = v["data"]["id"]
        print(f"sürüm {ver} oluşturuldu")

    # 3) build'i bağla
    die_if_err(_asc.api(f"/v1/appStoreVersions/{vid}/relationships/build", "PATCH",
                        {"data": {"type": "builds", "id": build_id}}), "build bağlama")
    print("✓ build sürüme bağlandı")

    # 4) notlar
    if a.notes:
        locs = die_if_err(_asc.api(
            f"/v1/appStoreVersions/{vid}/appStoreVersionLocalizations"), "lokalizasyonlar")
        for loc in locs.get("data", []):
            if loc["attributes"]["locale"] == "en-US":
                die_if_err(_asc.api(
                    f"/v1/appStoreVersionLocalizations/{loc['id']}", "PATCH",
                    {"data": {"type": "appStoreVersionLocalizations", "id": loc["id"],
                              "attributes": {"whatsNew": open(a.notes).read().strip()}}}),
                    "notlar")
                print("✓ sürüm notları yazıldı")

    # 5) incelemeye gönder (isteğe bağlı — Play'in aksine Apple API'den izin verir)
    if a.submit:
        sub = die_if_err(_asc.api("/v1/reviewSubmissions", "POST", {
            "data": {"type": "reviewSubmissions", "attributes": {"platform": "IOS"},
                     "relationships": {"app": {"data": {"type": "apps", "id": APP_ID}}}}}),
            "reviewSubmission")
        die_if_err(_asc.api("/v1/reviewSubmissionItems", "POST", {
            "data": {"type": "reviewSubmissionItems",
                     "relationships": {
                         "reviewSubmission": {"data": {"type": "reviewSubmissions",
                                                       "id": sub["data"]["id"]}},
                         "appStoreVersion": {"data": {"type": "appStoreVersions",
                                                      "id": vid}}}}}), "submission item")
        die_if_err(_asc.api(f"/v1/reviewSubmissions/{sub['data']['id']}", "PATCH", {
            "data": {"type": "reviewSubmissions", "id": sub["data"]["id"],
                     "attributes": {"submitted": True}}}), "submit")
        print("✓ İNCELEMEYE GÖNDERİLDİ")
    else:
        print("gönderilmedi (--submit ile gönderilir)")
