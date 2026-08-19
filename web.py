#!/usr/bin/env python3
"""Fabrika paneli yazma ucu — panelden gelen girdileri DB'ye yazar.

Panel statik HTML olarak üretilir; bu servis yalnız POST'ları alır, kaydeder ve
paneli anında yeniden üretir (kullanıcı yazdığını hemen görsün).

Güvenlik: panelin gizli yol jetonu URL'de zorunlu — statik sayfayı görebilen
yazabilir, göremeyen hiçbir şey yapamaz. Servis yalnız 127.0.0.1'e bağlanır,
dışarıya Caddy açar. Girdi uzunlukları sınırlı; app_id künye listesine karşı
doğrulanır (serbest metin DB'ye tablo/kolon adı olarak asla girmez).

systemd: factory-web.service  ·  Caddy: /{token}/api/* → 127.0.0.1:8099
"""
import json, os, subprocess, sys, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8099
TOKEN_FILE = "/opt/factory/panel_token"
PANEL_DIR = "/var/www/fabrika"
GENERATOR = "/opt/factory/panel/generate.py"
MAX_BODY = 16 * 1024
MAX_VALUE = 2000
ALLOWED_KEYS = {"repo", "pitch", "pricing", "firebase", "push", "ads"}
LIFECYCLE = {"idea", "build", "soak", "spark", "scale", "harvest", "killed"}


def load_env(path):
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)


load_env("/root/.factory.env")
TOKEN = open(TOKEN_FILE).read().strip() if os.path.exists(TOKEN_FILE) else ""


def psql(sql, params_ok=True):
    """ON_ERROR_STOP şart: onsuz psql hatalı ifadeden sonra da 0 döner ve
    yetki hatası 'başarılı' görünür — panel kaydettim der, hiçbir şey yazılmaz."""
    p = subprocess.run(["psql", "-v", "ON_ERROR_STOP=1",
                        os.environ["FACTORY_DATABASE_URL"], "-At", "-F", "\t"],
                       input=sql, capture_output=True, text=True)
    return p.returncode == 0, (p.stderr or p.stdout)[:500]


def known_apps():
    ok, out = psql("SELECT id FROM factory.apps;")
    return {r.strip() for r in out.split("\n") if r.strip()} if ok else set()


def esc(s):
    """SQL tek tırnak kaçışı — değerler yalnız literal olarak kullanılır."""
    return str(s).replace("'", "''")


def regenerate():
    subprocess.run([sys.executable, GENERATOR, os.path.join(PANEL_DIR, TOKEN)],
                   capture_output=True, text=True, timeout=120)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body="", ctype="text/plain; charset=utf-8", headers=None):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass  # journald zaten erişim logu tutuyor; gürültü yapma

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        parts = [p for p in path.split("/") if p]
        # beklenen: /{token}/api/{action}
        if len(parts) < 3 or parts[0] != TOKEN or parts[1] != "api":
            return self._send(404, "yok")
        action = parts[2]

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._send(413, "çok büyük")
        raw = self.rfile.read(length).decode("utf-8", "replace")
        form = {k: v[0][:MAX_VALUE] for k, v in urllib.parse.parse_qs(raw).items()}

        app = form.get("app", "")
        if app not in known_apps():
            return self._send(400, "bilinmeyen uygulama")

        if action == "intake":
            stmts = ["BEGIN;"]
            n = 0
            for k, v in form.items():
                if k in ALLOWED_KEYS and v.strip():
                    stmts.append(
                        "INSERT INTO factory.app_inputs (app_id,key,value) VALUES "
                        f"('{esc(app)}','{esc(k)}','{esc(v.strip())}') "
                        "ON CONFLICT (app_id,key) DO UPDATE SET "
                        "value=EXCLUDED.value, updated_at=now();")
                    n += 1
            stmts.append("COMMIT;")
            ok, err = psql("\n".join(stmts))
            if not ok:
                return self._send(500, f"kaydedilemedi: {err}")
            regenerate()
            return self._send(303, "", headers={"Location": f"/{TOKEN}/{app}.html#intake"})

        if action == "budget":
            # Bütçe kullanıcının parasıdır: yalnız sayısal, pozitif değer kabul et.
            stmts = ["BEGIN;"]
            n = 0
            for k in ("budget_daily_usd", "budget_spark_cap_usd"):
                v = form.get(k, "").strip().replace(",", ".")
                if not v:
                    continue
                try:
                    num = float(v)
                except ValueError:
                    return self._send(400, f"{k} sayı olmalı")
                if num <= 0 or num > 100000:
                    return self._send(400, f"{k} 0-100000 aralığında olmalı")
                stmts.append(
                    "INSERT INTO factory.app_inputs (app_id,key,value) VALUES "
                    f"('{esc(app)}','{k}','{num:g}') "
                    "ON CONFLICT (app_id,key) DO UPDATE SET "
                    "value=EXCLUDED.value, updated_at=now();")
                n += 1
            stmts.append("COMMIT;")
            if not n:
                return self._send(400, "değer girilmedi")
            ok, err = psql("\n".join(stmts))
            if not ok:
                return self._send(500, f"kaydedilemedi: {err}")
            regenerate()
            return self._send(303, "", headers={"Location": f"/{TOKEN}/{app}.html#kota"})

        if action == "status":
            # Yaşam döngüsü aşaması operasyonel durumdur ve sık değişir →
            # sahibi DB'dir (künye yalnız eşik/bütçe gibi yapılandırmayı tutar).
            st = form.get("status", "")
            if st not in LIFECYCLE:
                return self._send(400, "geçersiz aşama")
            ok, err = psql(f"UPDATE factory.apps SET status='{esc(st)}' "
                           f"WHERE id='{esc(app)}';")
            if not ok:
                return self._send(500, f"güncellenemedi: {err}")
            regenerate()
            return self._send(303, "", headers={"Location": f"/{TOKEN}/{app}.html"})

        if action == "refresh":
            regenerate()
            return self._send(303, "", headers={"Location": f"/{TOKEN}/{app}.html"})

        if action == "done":
            mid = form.get("id", "")
            if not mid.isdigit():
                return self._send(400, "geçersiz id")
            ok, err = psql("UPDATE factory.messages SET status='done' "
                           f"WHERE id={int(mid)};")
            if not ok:
                return self._send(500, f"güncellenemedi: {err}")
            regenerate()
            return self._send(303, "", headers={"Location": f"/{TOKEN}/{app}.html#mesaj"})

        if action == "message":
            body = form.get("body", "").strip()
            if not body:
                return self._send(400, "boş mesaj")
            ok, err = psql("INSERT INTO factory.messages (app_id,direction,body) VALUES "
                           f"('{esc(app)}','user','{esc(body)}');")
            if not ok:
                return self._send(500, f"kaydedilemedi: {err}")
            regenerate()
            return self._send(303, "", headers={"Location": f"/{TOKEN}/{app}.html#mesaj"})

        if action == "creative":
            # Kreatif kararı: onay/ret/revizyon. Ret ve revizyonda not ZORUNLU —
            # o notlar brief üreticisine geri beslenen öğrenme verisidir.
            cid = form.get("id", "")
            verdict = form.get("verdict", "")
            note = form.get("note", "").strip()
            if not cid.isdigit() or verdict not in ("approved", "rejected", "revision_requested"):
                return self._send(400, "geçersiz karar")
            if verdict != "approved" and not note:
                return self._send(400, "ret/revizyon için gerekçe zorunlu")
            # idempotent: yalnız pending kayıt karar alır; olay logu da yalnız
            # UPDATE gerçekten satır değiştirdiyse yazılır (ikinci tık tam no-op).
            nval = "NULL" if not note else "'" + esc(note) + "'"
            ok, err = psql(
                f"WITH upd AS (UPDATE factory.creatives SET review_status='{verdict}', "
                f"review_note={nval}, reviewed_at=now() "
                f"WHERE id={int(cid)} AND app_id='{esc(app)}' "
                "AND review_status='pending' RETURNING id) "
                f"INSERT INTO factory.review_events (creative_id,action,note) "
                f"SELECT id,'{verdict}',{nval} FROM upd;")
            if not ok:
                return self._send(500, f"kaydedilemedi: {err}")
            regenerate()
            return self._send(303, "", headers={"Location": f"/{TOKEN}/{app}.html#kreatif"})

        return self._send(404, "bilinmeyen işlem")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("panel jetonu yok: " + TOKEN_FILE)
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
