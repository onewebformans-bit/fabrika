"""Google servis hesabı yardımcısı — BigQuery, Play ve Reporting API'leri için.

Tek servis hesabı (Play SA, coparent-9ca78) üç API'yi de karşılar; kapsam
(scope) istenen API'ye göre değişir. Kütüphane bağımlılığı YOK: JWT'yi openssl
ile imzalar, token'ı OAuth uçundan alır.

    from _google import gapi
    gapi(SA_JSON, SCOPE_BQ, url)                  # GET
    gapi(SA_JSON, SCOPE_BQ, url, body={...})      # POST
"""
import base64, json, subprocess, time, urllib.error, urllib.parse, urllib.request

SCOPE_BQ = "https://www.googleapis.com/auth/bigquery"
SCOPE_PLAY = "https://www.googleapis.com/auth/androidpublisher"
SCOPE_REPORT = "https://www.googleapis.com/auth/playdeveloperreporting"

_cache = {}


def _b64(d):
    return base64.urlsafe_b64encode(d).rstrip(b"=")


def token(sa_path, scope):
    key = (sa_path, scope)
    tok, exp = _cache.get(key, (None, 0))
    if tok and time.time() < exp - 60:
        return tok
    info = json.load(open(sa_path))
    now = int(time.time())
    hdr = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claim = _b64(json.dumps({
        "iss": info["client_email"], "scope": scope,
        "aud": "https://oauth2.googleapis.com/token",
        "iat": now, "exp": now + 3600}).encode())
    signing = hdr + b"." + claim
    sig_in = "/tmp/_gsign_%d" % time.time_ns()
    key_f = "/tmp/_gkey_%d" % time.time_ns()
    try:
        open(sig_in, "wb").write(signing)
        open(key_f, "w").write(info["private_key"])
        der = subprocess.run(["openssl", "dgst", "-sha256", "-sign", key_f, sig_in],
                             capture_output=True).stdout
    finally:
        import os
        for f in (sig_in, key_f):
            if os.path.exists(f):
                os.unlink(f)
    jwt = (signing + b"." + _b64(der)).decode()
    data = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt}).encode()
    r = json.load(urllib.request.urlopen("https://oauth2.googleapis.com/token",
                                         data=data, timeout=30))
    _cache[key] = (r["access_token"], now + int(r.get("expires_in", 3600)))
    return r["access_token"]


def gapi(sa_path, scope, url, body=None, method=None):
    """Hata fırlatmaz — {'_err': kod, '_body': metin} döndürür.
    Toplayıcılar bir API kapalıyken de çalışmaya devam etmeli."""
    try:
        t = token(sa_path, scope)
    except Exception as e:
        return {"_err": "auth", "_body": str(e)[:200]}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {t}",
                 **({"Content-Type": "application/json"} if body else {})},
        method=method or ("POST" if body else "GET"))
    try:
        return json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        return {"_err": e.code, "_body": e.read()[:300].decode(errors="replace")}
    except Exception as e:
        return {"_err": "net", "_body": str(e)[:200]}
