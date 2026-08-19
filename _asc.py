"""App Store Connect API — paylaşılan istemci (takım geneli anahtar).

Env: ASC_KEY_PATH, ASC_KEY_ID, ASC_ISSUER_ID  (/root/.asc.env)
Kullanım:  exec(open('_asc.py').read())  →  T (bearer), api(path)
"""
import base64, json, os, subprocess, time, urllib.error, urllib.request

_KEY = os.environ.get("ASC_KEY_PATH", "")
_KID = os.environ.get("ASC_KEY_ID", "")
_ISS = os.environ.get("ASC_ISSUER_ID", "")


def _b64(d):
    return base64.urlsafe_b64encode(d).rstrip(b'=')


def _raw(der):
    """openssl'in DER imzasını JWS'in beklediği r||s biçimine çevirir."""
    i = 2
    assert der[i] == 2; l = der[i + 1]; r = der[i + 2:i + 2 + l]; i = i + 2 + l
    assert der[i] == 2; l2 = der[i + 1]; s = der[i + 2:i + 2 + l2]
    return r.lstrip(b'\0').rjust(32, b'\0') + s.lstrip(b'\0').rjust(32, b'\0')


def _token():
    h = _b64(json.dumps({"alg": "ES256", "kid": _KID, "typ": "JWT"}).encode())
    p = _b64(json.dumps({"iss": _ISS, "iat": int(time.time()),
                         "exp": int(time.time()) + 900,
                         "aud": "appstoreconnect-v1"}).encode())
    sg = h + b'.' + p
    tmp = "/tmp/_asc_sign_%d" % os.getpid()
    open(tmp, 'wb').write(sg)
    der = subprocess.run(['openssl', 'dgst', '-sha256', '-sign', _KEY, tmp],
                         capture_output=True).stdout
    os.unlink(tmp)
    return (sg + b'.' + _b64(_raw(der))).decode()


T = _token() if _KEY and os.path.exists(_KEY) else ""


def api(path, method="GET", body=None):
    data = json.dumps(body).encode() if body else None
    headers = {"Authorization": f"Bearer {T}"}
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request("https://api.appstoreconnect.apple.com" + path,
                                 data=data, headers=headers, method=method)
    try:
        return json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        return {"_err": e.code, "_body": e.read()[:300].decode(errors='replace')}
