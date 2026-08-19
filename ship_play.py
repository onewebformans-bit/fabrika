#!/usr/bin/env python3
"""Play'e sürüm gönderimi — bugün elle yapılan akışın kalıcı hâli.

Kullanım (droplet'te, SA anahtarının olduğu yerde):
  python3 ship_play.py <app> <aab-yolu> --track internal
  python3 ship_play.py <app> --track production --version-code 11 --notes notes.txt

internal: AAB'yi yükler ve internal release yapar (upload-key testi burada olur).
production: var olan versionCode'u production'a terfi ettirir; asıl "incelemeye
gönder" düğmesi Play Console'da insanda kalır (Google öyle istiyor).
"""
import argparse, json, os, sys, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _google import token  # noqa: E402
from gates import yaml_lite, APPS_DIR  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("app")
ap.add_argument("aab", nargs="?")
ap.add_argument("--track", choices=["internal", "production"], default="internal")
ap.add_argument("--version-code", help="production terfisi için (AAB verilmediyse)")
ap.add_argument("--name", help="release adı, örn '11 (1.6.0)'")
ap.add_argument("--notes", help="en-US sürüm notları dosyası")
a = ap.parse_args()

cfg = yaml_lite(os.path.join(APPS_DIR, a.app + ".yaml"))
PKG = (cfg.get("store") or {}).get("play_package") or cfg.get("bundle_id")
SA = os.environ.get("FACTORY_SA") or (cfg.get("analytics") or {}).get("service_account")
if not (PKG and SA):
    raise SystemExit("künyede store.play_package / analytics.service_account eksik")

BASE = f"https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{PKG}"
UP = f"https://androidpublisher.googleapis.com/upload/androidpublisher/v3/applications/{PKG}"
T = token(SA, "https://www.googleapis.com/auth/androidpublisher")


def api(m, url, body=None, ctype="application/json", raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body else None)
    r = urllib.request.Request(url, method=m, data=data,
                               headers={"Authorization": f"Bearer {T}", "Content-Type": ctype})
    try:
        return json.load(urllib.request.urlopen(r, timeout=300))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"HATA {e.code} {url}\n{e.read()[:600].decode(errors='replace')}")


ed = api("POST", f"{BASE}/edits")["id"]

if a.track == "internal":
    if not a.aab:
        raise SystemExit("internal için AAB yolu gerekli")
    print("AAB yükleniyor (upload-key testi burada)…")
    b = api("POST", f"{UP}/edits/{ed}/bundles?uploadType=media",
            ctype="application/octet-stream", raw=open(a.aab, "rb").read())
    vc = str(b["versionCode"])
    print(f"  ✓ kabul edildi — versionCode {vc}")
else:
    vc = a.version_code
    if not vc:
        raise SystemExit("production için --version-code gerekli")

release = {"name": a.name or vc, "versionCodes": [vc], "status": "completed"}
if a.notes:
    release["releaseNotes"] = [{"language": "en-US", "text": open(a.notes).read().strip()}]
api("PUT", f"{BASE}/edits/{ed}/tracks/{a.track}", body={"track": a.track, "releases": [release]})
api("POST", f"{BASE}/edits/{ed}:commit")
print(f"✓ {a.track} ← versionCode {vc} COMMIT edildi")
if a.track == "production":
    print("  SON ADIM İNSANDA: Play Console → 'değişikliği incelemeye gönder'")
