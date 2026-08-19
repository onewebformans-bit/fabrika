#!/usr/bin/env python3
# Fabrika Paneli üretici — API droplet'inde çalışır (cron: toplayıcıdan sonra).
# factory DB'yi okur, metrikleri yorumlar, /var/www/fabrika/index.html basar.
# Grafana yerine: okunur, yorumlu, tek bakışta anlaşılır kokpit.
import json, os, subprocess, sys
from datetime import date, timedelta
from string import Template
from urllib.parse import urlparse

# --- veri ---------------------------------------------------------------------
def load_env(path="/root/.factory.env"):
    if not os.path.exists(path):   # lokal geliştirmede env dışarıdan gelir
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)

def q(sql):
    # ponytail: psycopg bagimliligi yerine psql — droplet'te zaten kurulu
    out = subprocess.run(
        ["psql", os.environ["FACTORY_DATABASE_URL"], "-At", "-F", "\t", "-c", sql],
        capture_output=True, text=True, check=True)
    return [r.split("\t") for r in out.stdout.strip().split("\n") if r]

load_env()
apps = q("SELECT id, name, status, COALESCE(notes,'') FROM factory.apps ORDER BY id")
rows = q("""SELECT app_id, date, metric, segment, value
            FROM factory.app_metrics_daily
            WHERE date > current_date - 90 ORDER BY date""")
# Modül durumları: boru hattı aşamaları + intake maddeleri (pipeline.publish yazar)
status_rows = q("""SELECT app_id, kind, key, ord, ok, COALESCE(label,''), COALESCE(note,'')
                   FROM factory.app_status ORDER BY app_id, kind, ord""")
# Son kapı kararı — kota motorunun çıktısı
gate_rows = q("""SELECT DISTINCT ON (app_id) app_id, payload::text
                 FROM factory.decisions WHERE kind='gate'
                 ORDER BY app_id, created_at DESC""")
# Panelden girilen intake cevapları ve beyne yazılan istekler
input_rows = q("SELECT app_id, key, value FROM factory.app_inputs")
msg_rows = q("""SELECT app_id, direction, body, status, created_at::date::text, id::text
                FROM factory.messages ORDER BY created_at DESC LIMIT 60""")

# Panelin yazma ucu için gizli jeton (form action'ları buna gider)
TOKEN = ""
for p in ("/opt/factory/panel_token",):
    if os.path.exists(p):
        TOKEN = open(p).read().strip()

def esc_html(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))

# --- metrik yardimcilari ------------------------------------------------------
def series(app, metric, segment=""):
    return [(r[1], float(r[4])) for r in rows if r[0] == app and r[2] == metric and r[3] == segment]

def latest(app, metric, segment=""):
    s = series(app, metric, segment)
    return s[-1][1] if s else None

def latest_date(app):
    ds = [r[1] for r in rows if r[0] == app]
    return max(ds) if ds else None

def delta(app, metric):
    s = series(app, metric)
    if len(s) < 2: return None
    return s[-1][1] - s[-2][1]

LIFECYCLE = ["idea", "build", "soak", "spark", "scale", "harvest"]
LIFECYCLE_TR = {"idea": "Fikir", "build": "Yapım", "soak": "Isınma", "spark": "Kıvılcım",
                "scale": "Ölçek", "harvest": "Hasat", "killed": "Kapatıldı"}

# Kıvılcım eşikleri (factory/playbooks/core.md ile aynı — degisirse ikisini birden guncelle)
def pct(v):
    return "—" if v is None else f"%{v*100:.0f}"


# Kıvılcım eşikleri artık GERÇEK veriye bakar; kaynak yoksa "—" ve nedeni yazar.
def spark_rows_for(aid):
    return [
        ("Onboarding tamamlama", pct(latest(aid, "onboarding_completion")), "≥ %60",
         "BigQuery funnel" if latest(aid, "onboarding_completion") is not None
         else "BigQuery export açılınca dolacak"),
        ("Paywall görüntüleme", pct(latest(aid, "paywall_view_rate")), "≥ %70",
         "BigQuery funnel" if latest(aid, "paywall_view_rate") is not None
         else "BigQuery export açılınca dolacak"),
        ("Çökmesiz oturum", pct(latest(aid, "crash_free_rate", "android")), "≥ %99.5",
         "Play Reporting" if latest(aid, "crash_free_rate", "android") is not None
         else "Play Reporting API açılınca dolacak"),
        ("Görünen aramalar", f"{int(latest(aid, 'aso_visible_searches') or 0)}/5", "5/5",
         "ASO takibi (haftalık)"),
    ]


SPARK_ROWS = [
    ("Onboarding tamamlama", None, "≥ %60", "Amplitude funnel bağlanınca ölçülecek"),
    ("Trial başlatma (install'a)", None, "≥ %5", "Mağaza verisi + trial offer sonrası"),
    ("Trial→paid dönüşüm", None, "≥ %35", "İlk trial kohortu tamamlanınca"),
    ("D1 retention", None, "≥ %25", "Amplitude bağlanınca"),
]

# --- uygulama kartlari + yorumlar ---------------------------------------------
def comments_for(app_id, name):
    c = []
    users = latest(app_id, "users_total"); new = latest(app_id, "users_new")
    trials = latest(app_id, "trial_starts")
    ld = latest_date(app_id)
    if ld is None:
        c.append(("warn", "Toplayıcı bu uygulama için henüz veri yazmadı — boru hattını kontrol et."))
        return c
    if ld < (date.today() - timedelta(days=1)).isoformat():
        c.append(("warn", f"Son veri {ld} — toplayıcı gecikmiş olabilir (her gece 00:10 UTC koşar)."))
    if users == 0 or users is None:
        c.append(("info", "Henüz kullanıcı yok. Mağaza yayını çok yeni; indeksleme 24-72 saat sürer. "
                          "Bu aşamada normal — ilk hedef SOAK kapısı için organik ilk kullanıcılar."))
    else:
        c.append(("good", f"Toplam {int(users)} kullanıcı."))
        if new and new > 0: c.append(("good", f"Son gün +{int(new)} yeni kullanıcı."))
        elif new == 0: c.append(("info", "Son gün yeni kullanıcı gelmedi."))
    subs = [r for r in rows if r[0] == app_id and r[2] == "subs_snapshot" and r[1] == ld]
    active = sum(float(r[4]) for r in subs if "status=active" in r[3] or "status=trialing" in r[3])
    if active > 0:
        c.append(("good", f"🎉 {int(active)} aktif/trial abonelik var — gelir akışı başladı."))
    elif users and users > 0:
        c.append(("info", "Henüz abonelik yok. Onboarding→paywall funnel'ı yeni build mağazaya gidince akacak."))
    if trials and trials > 0:
        c.append(("good", f"Son gün {int(trials)} trial başlangıcı."))

    # --- mağaza cephesi (ASC toplayıcısı) ---
    store = [r for r in rows if r[0] == app_id and r[1] == ld]
    live = next((r[3] for r in store if r[2] == "app_version_live"), None)
    inrev = next((r[3] for r in store if r[2] == "app_version_in_review"), None)
    if live:
        v = live.split("v=")[-1]
        c.append(("info", f"App Store'da yayında: v{v}."))
    if inrev:
        parts = inrev.split(",")[-1]
        c.append(("warn", f"Review'da bekleyen sürüm: {parts} — sonuçlanmadan yeni sürüm gönderilemez."))
    rating = next((float(r[4]) for r in store if r[2] == "app_rating_avg"), None)
    rcount = next((float(r[4]) for r in store if r[2] == "app_rating_count"), None)
    if rating:
        c.append(("good", f"App Store puanı {rating:.1f} ({int(rcount or 0)} yorum)."))
    elif live:
        c.append(("info", "Henüz App Store yorumu yok — ilk yorumlar sıralamayı doğrudan etkiler."))
    return c

def chart_data(app_id, metric, segment=""):
    s = series(app_id, metric, segment)
    return {"labels": [d[5:] for d, _ in s], "values": [v for _, v in s]}

app_blocks, data_js, app_cards = [], {}, []
for app_row in apps:
    # psql boş son sütunu kırpabiliyor — sabit sayıya güvenme
    aid, name, status = app_row[0], app_row[1], app_row[2]
    users = latest(aid, "users_total"); hh = latest(aid, "households_total")
    new = latest(aid, "users_new"); tr = latest(aid, "trial_starts")
    ld = latest_date(aid) or "—"
    subs = [r for r in rows if r[0] == aid and r[2] == "subs_snapshot" and r[1] == ld]
    active = sum(float(r[4]) for r in subs if "status=active" in r[3] or "status=trialing" in r[3])
    dlt = delta(aid, "users_total")
    dlt_html = "" if dlt is None else f'<span class="delta {"up" if dlt>0 else "flat"}">{"+" if dlt>0 else ""}{int(dlt)}</span>'
    fmt = lambda v: "—" if v is None else str(int(v))

    # Aşamalar tıklanabilir: uygulamanın yaşam döngüsünü panelden değiştir.
    # Aşama operasyonel durumdur, sahibi DB'dir (künye yalnız eşikleri tutar).
    cur = LIFECYCLE.index(status) if status in LIFECYCLE else -1
    steps = "".join(
        f'<form class="stepf" method="post" action="/{TOKEN}/api/status">'
        f'<input type="hidden" name="app" value="{aid}">'
        f'<input type="hidden" name="status" value="{s}">'
        f'<button class="step {"done" if cur > i else "now" if cur == i else ""}" '
        f'type="submit" title="{LIFECYCLE_TR[s]} aşamasına al">{LIFECYCLE_TR[s]}</button>'
        f'</form>'
        for i, s in enumerate(LIFECYCLE))

    comments = "".join(
        f'<li class="c-{kind}"><span class="dot"></span>{txt}</li>'
        for kind, txt in comments_for(aid, name))

    spark = "".join(
        f'<tr><td>{m}</td><td class="num">{"—" if cur is None else cur}</td>'
        f'<td class="num">{target}</td><td class="muted">{note}</td></tr>'
        for m, cur, target, note in spark_rows_for(aid))

    # --- modüller: boru hattı aşamaları + intake formu -------------------------
    def status_list(kind):
        return [r for r in status_rows if r[0] == aid and r[1] == kind]

    STAGE_TR = {"instrument": "Analitik etiketleri", "backend": "Backend + faturalama",
                "signing": "İmzalama (iOS + Android)", "ship": "Mağazaya gönderim",
                "intel": "Rakip ASO/reklam istihbaratı", "aso": "Kendi ASO paketimiz",
                "creative": "Mağaza görselleri + kreatifler", "ads": "Reklam yönetimi",
                "watch": "Ölçüm + kota kararı"}
    st_rows = status_list("stage")
    stages_html = "".join(
        f'<li class="{"ok" if r[4] == "t" else "todo"}">'
        f'<span class="dot"></span><b>{STAGE_TR.get(r[2], r[2])}</b>'
        f'<span class="muted"> — {r[6]}</span></li>'
        for r in st_rows) or '<li class="muted">durum yayınlanmadı (factory status çalıştır)</li>'
    st_done = sum(1 for r in st_rows if r[4] == "t")

    in_rows = status_list("intake")
    in_done = sum(1 for r in in_rows if r[4] == "t")

    # İntake artık FORM: cevapları panelden gir, fabrika DB'ye yazsın.
    # Anahtar sırası factory.py INTAKE listesiyle aynı (1..6).
    IN_KEYS = ["repo", "pitch", "pricing", "firebase", "push", "ads"]
    IN_PH = {
        "repo": "~/Projects/Roadfolk",
        "pitch": "yol gezisi planlayıcı — rota, mola ve bütçe tek yerde",
        "pricing": "8 / 79 / 7   (aylık $ / yıllık $ / deneme günü)",
        "firebase": "firebase proje adı",
        "push": "OneSignal App ID",
        "ads": "Meta reklam hesabı ID (kart bağlı)",
    }
    given = {r[1]: r[2] for r in input_rows if r[0] == aid}
    fields = []
    for i, r in enumerate(in_rows):
        key = IN_KEYS[i] if i < len(IN_KEYS) else f"k{i}"
        val = esc_html(given.get(key, ""))
        done = r[4] == "t" or bool(val)
        fields.append(
            f'<label class="fld {"ok" if done else ""}">'
            f'<span class="fl">{r[2]}. {esc_html(r[5])}</span>'
            f'<input name="{key}" value="{val}" placeholder="{esc_html(IN_PH.get(key,""))}">'
            + (f'<span class="fh">{esc_html(r[6])}</span>' if r[6] and not done else '')
            + '</label>')
    intake_html = (
        f'<form id="intake" method="post" action="/{TOKEN}/api/intake">'
        f'<input type="hidden" name="app" value="{aid}">'
        + "".join(fields)
        + '<button type="submit">Kaydet</button></form>'
    ) if in_rows else '<div class="muted">intake yayınlanmadı</div>'

    # --- beyne mesaj kutusu ----------------------------------------------------
    msgs = [m for m in msg_rows if m[0] == aid]
    msg_list = "".join(
        f'<li class="m-{esc_html(m[1])}"><span class="mw">'
        f'{"Sen" if m[1] == "user" else "Beyin"}</span>'
        f'<span class="mb">{esc_html(m[2])}</span>'
        f'<span class="muted">{esc_html(m[4])} · {esc_html(m[3])}</span></li>'
        for m in msgs[:8]) or '<li class="muted">henüz istek yok</li>'
    # Hızlı aksiyonlar: tek tıkla istek yaz. Panelden iş başlatmanın en kısa yolu;
    # her biri fabrikaya mesaj düşer, ben oradan alıp uygularım.
    QUICK = [
        ("ASO'yu uygula", "ASO paketini mağaza metadatasına uygula ve yeni sürüme hazırla"),
        ("Yeni sürüm gönder", "Son değişikliklerle imzalı build üret ve mağazaya gönder"),
        ("Rakipleri tara", "Rakip ASO ve reklam istihbaratını yenile"),
        ("Kreatif üret", "Rakip kazanan reklamlarından yola çıkarak kreatif seti üret"),
        ("Bütçeyi artır", "Kota kapıları açıksa reklam bütçesini bir kademe artır"),
        ("Durdur", "Bu uygulamanın reklam harcamasını durdur"),
    ]
    quick_html = "".join(
        f'<form class="qa" method="post" action="/{TOKEN}/api/message">'
        f'<input type="hidden" name="app" value="{aid}">'
        f'<input type="hidden" name="body" value="{esc_html(body)}">'
        f'<button type="submit">{esc_html(label)}</button></form>'
        for label, body in QUICK)

    open_msgs = [m for m in msgs if m[3] == "new"]
    msg_list = "".join(
        f'<li class="m-{esc_html(m[1])}"><span class="mw">'
        f'{"Sen" if m[1] == "user" else "Beyin"}</span>'
        f'<span class="mb">{esc_html(m[2])}</span>'
        f'<span class="muted">{esc_html(m[4])} · {esc_html(m[3])}</span>'
        + (f'<form class="mdone" method="post" action="/{TOKEN}/api/done">'
           f'<input type="hidden" name="app" value="{aid}">'
           f'<input type="hidden" name="id" value="{esc_html(m[5])}">'
           f'<button type="submit">bitti</button></form>' if m[3] == "new" else "")
        + '</li>'
        for m in msgs[:8]) or '<li class="muted">henüz istek yok</li>'

    msgbox = (
        f'<div class="quick">{quick_html}</div>'
        f'<form id="mesaj" method="post" action="/{TOKEN}/api/message">'
        f'<input type="hidden" name="app" value="{aid}">'
        f'<textarea name="body" rows="3" placeholder="…ya da kendi isteğini yaz"></textarea>'
        f'<button type="submit">Gönder</button></form>'
        f'<ul class="msgs">{msg_list}</ul>')

    # --- kapı kararı (kota motoru) ---------------------------------------------
    gate = next((json.loads(g[1]) for g in gate_rows if g[0] == aid), None)
    if gate:
        d = gate.get("decision", "?")
        cls = {"KILL": "bad", "HOLD": "warn", "SCALE": "good"}.get(d, "info")
        gate_html = (f'<div class="gate g-{cls}"><b>{d}</b>'
                     + "".join(f"<div>{w}</div>" for w in gate.get("why", [])) + "</div>")
    else:
        gate_html = '<div class="muted">kapı motoru henüz karar yazmadı</div>'

    data_js[aid] = {"growth": chart_data(aid, "users_total"),
                    "new": chart_data(aid, "users_new"),
                    "trial": chart_data(aid, "trial_starts"),
                    "downloads": chart_data(aid, "store_downloads", "ios")}
    # satış metrikleri platform segmentiyle yazılır ('ios'), toplam sütunu yok
    dl_total = sum(v for _, v in series(aid, "store_downloads", "ios"))

    app_blocks.append(f"""
    <section class="app">
      <div class="app-head">
        <h2>{name}</h2><span class="badge">{LIFECYCLE_TR.get(status, status)}</span>
        <span class="muted">son veri: {ld}</span>
      </div>
      <div class="lifecycle">{steps}</div>
      <div class="cards">
        <div class="card"><div class="label">Toplam kullanıcı</div><div class="value">{fmt(users)} {dlt_html}</div></div>
        <div class="card"><div class="label">Hane</div><div class="value">{fmt(hh)}</div></div>
        <div class="card"><div class="label">Aktif + trial abonelik</div><div class="value">{fmt(active if subs else None)}</div></div>
        <div class="card"><div class="label">Son gün yeni kullanıcı</div><div class="value">{fmt(new)}</div></div>
        <div class="card"><div class="label">Son gün trial</div><div class="value">{fmt(tr)}</div></div>
        <div class="card"><div class="label">Mağaza indirmesi (toplam)</div><div class="value">{int(dl_total)}</div></div>
      </div>
      <!-- Sekmeler: her alan kendi kategorisinde. Grafikler yalnız Özet'te —
           gizli sekmede Chart.js sıfır boyutlu canvas'a çizerdi. -->
      <nav class="tabs">
        <button class="tab active" data-t="ozet">Özet</button>
        <button class="tab" data-t="hat">Boru hattı <i>{st_done}/{len(st_rows) or 9}</i></button>
        <button class="tab" data-t="kurulum">Kurulum <i>{in_done}/{len(in_rows) or 6}</i></button>
        <button class="tab" data-t="kota">Kota</button>
        <button class="tab" data-t="istek">İstekler{f' <i>{len(open_msgs)}</i>' if open_msgs else ''}</button>
      </nav>

      <div class="panel show" data-p="ozet">
        <div class="charts">
          <div class="chart"><h3>Mağaza indirmeleri</h3><canvas id="d-{aid}"></canvas></div>
          <div class="chart"><h3>Kullanıcı büyümesi</h3><canvas id="g-{aid}"></canvas></div>
          <div class="chart"><h3>Günlük yeni kullanıcı</h3><canvas id="n-{aid}"></canvas></div>
          <div class="chart"><h3>Trial başlangıçları</h3><canvas id="t-{aid}"></canvas></div>
        </div>
        <h3>Beyin notları</h3><ul class="comments">{comments}</ul>
      </div>

      <div class="panel" data-p="hat">
        <h3>Boru hattı modülleri <span class="muted">{st_done}/{len(st_rows) or 9} aşama tamam</span></h3>
        <ul class="checks">{stages_html}</ul>
      </div>

      <div class="panel" data-p="kurulum">
        <h3>İntake — buraya gir <span class="muted">{in_done}/{len(in_rows) or 6}</span></h3>
        {intake_html}
      </div>

      <div class="panel" data-p="kota">
        <h3>Bütçe tavanları — sen belirlersin</h3>
        <form class="budget" method="post" action="/{TOKEN}/api/budget">
          <input type="hidden" name="app" value="{aid}">
          <div class="brow">
            <label class="fld"><span class="fl">Günlük tavan ($)</span>
              <input name="budget_daily_usd" inputmode="decimal"
                     value="{esc_html(given.get('budget_daily_usd', ''))}"
                     placeholder="varsayılan: 50"></label>
            <label class="fld"><span class="fl">Toplam test tavanı ($)</span>
              <input name="budget_spark_cap_usd" inputmode="decimal"
                     value="{esc_html(given.get('budget_spark_cap_usd', ''))}"
                     placeholder="varsayılan: 2500"></label>
          </div>
          <button type="submit">Kaydet</button>
          <span class="muted" style="font-size:11.5px;margin-left:10px">
            Toplam tavan dolduğunda ve kıvılcım yoksa fabrika DURDURMA önerir.
            Boş bırakılan alan fabrika varsayılanını kullanır.</span>
        </form>
        <h3 style="margin-top:18px">Kota kararı</h3>
        {gate_html}
        <h3 style="margin-top:18px">Kıvılcım eşikleri (SPARK kapısı)</h3>
        <table class="spark"><tr><th>Metrik</th><th>Mevcut</th><th>Hedef</th><th>Not</th></tr>{spark}</table>
      </div>

      <div class="panel" data-p="istek">
        <h3>Fabrikaya istek</h3>
        {msgbox}
      </div>
    </section>""")

    # giriş sayfası kartı için özet — detay bloğunun aynısını taşır
    cmts = comments_for(aid, name)
    app_cards.append({
        "id": aid, "name": name, "status": status,
        "downloads": int(dl_total), "users": fmt(users),
        "subs": fmt(active if subs else None),
        "stages_done": st_done, "stages_total": len(st_rows) or 9,
        "intake_done": in_done, "intake_total": len(in_rows) or 6,
        "gate": ({"d": gate["decision"],
                  "cls": {"KILL": "bad", "HOLD": "warn", "SCALE": "good"}
                         .get(gate["decision"], "info")} if gate else None),
        # kartta tek satır: en önemli yorum (uyarı varsa o, yoksa ilki)
        "headline": next((t for k, t in cmts if k == "warn"),
                         cmts[0][1] if cmts else ""),
        "detail": app_blocks[-1],
        "open_msgs": len(open_msgs),
        "stale": (ld == "—" or ld < (date.today() - timedelta(days=2)).isoformat()),
    })

# --- bekleyen isler (v0: elle; beyin devreye girince otomatik) -----------------
TODOS = [
    "Mağaza sırları (Apple/Play/OneSignal) → faturalama doğrulaması",
    "ASC intro offer + Play 'trial' etiketli offer tanımı",
    "Yeni mobil build (onboarding + yeni API) mağazaya",
    "Amplitude funnel bağlantısı (onboarding→paywall ölçümü)",
    "Mağaza indirme/sayfa verisi (ASC + Play API)",
]
todos_html = "".join(f"<li>{t}</li>" for t in TODOS)

GEN = subprocess.run(["date", "-u", "+%d %B %Y %H:%M UTC"],
                     capture_output=True, text=True).stdout.strip()
SHELL = Template(open(os.path.join(os.path.dirname(__file__), "template.html")).read())


def page(title, body, data=None):
    return SHELL.safe_substitute(TITLE=title, BODY=body,
                                 DATA=json.dumps(data or {}, ensure_ascii=False))


# --- 1) GİRİŞ SAYFASI: uygulama listesi ---------------------------------------
# Tek bakışta portföy: her uygulama bir kart, kart tıklanınca detay sayfası.
cards = []
for a in app_cards:
    g = a["gate"]
    gate_chip = (f'<span class="chip c-{g["cls"]}">{g["d"]}</span>' if g else "")
    bar = int(100 * a["stages_done"] / max(a["stages_total"], 1))
    flags = ""
    if a["open_msgs"]:
        flags += f'<span class="chip c-info">{a["open_msgs"]} açık istek</span>'
    if a["stale"]:
        flags += '<span class="chip c-warn">veri eski</span>'
    cards.append(f"""
    <a class="appcard" href="{a['id']}.html">
      <div class="ac-head">
        <h2>{a['name']}</h2>
        <span class="badge">{LIFECYCLE_TR.get(a['status'], a['status'])}</span>
        {gate_chip}{flags}
      </div>
      <div class="ac-nums">
        <div><b>{a['downloads']}</b><span>indirme</span></div>
        <div><b>{a['users']}</b><span>kullanıcı</span></div>
        <div><b>{a['subs']}</b><span>abonelik</span></div>
      </div>
      <div class="ac-prog"><div class="bar"><i style="width:{bar}%"></i></div>
        <span class="muted">boru hattı {a['stages_done']}/{a['stages_total']} ·
        intake {a['intake_done']}/{a['intake_total']}</span></div>
      <div class="ac-note">{a['headline']}</div>
      <div class="ac-go">detaya gir →</div>
    </a>""")

# Giriş sayfası gelen kutusu: tüm uygulamalardaki açık istekler tek yerde.
app_name = {a["id"]: a["name"] for a in app_cards}
open_all = [m for m in msg_rows if m[3] == "new"]
inbox_html = ("" if not open_all else
              '<section class="todos"><h2>Açık istekler ('
              + str(len(open_all)) + ')</h2><ul class="msgs">'
              + "".join(
                  f'<li class="m-{esc_html(m[1])}">'
                  f'<span class="mw">{esc_html(app_name.get(m[0], m[0] or "genel"))}</span>'
                  f'<span class="mb">{esc_html(m[2])}</span>'
                  f'<span class="muted">{esc_html(m[4])}</span></li>'
                  for m in open_all[:10])
              + '</ul></section>')

index_body = f"""
  <header>
    <h1>🏭 Mobil Fabrika</h1>
    <span class="muted">{len(app_cards)} uygulama · son üretim: {GEN} ·
      veri her gece 00:10 UTC'de tazelenir</span>
  </header>
  <div class="grid">{''.join(cards)}</div>
  {inbox_html}
  <section class="todos">
    <h2>Bekleyen işler</h2>
    <ol>{todos_html}</ol>
  </section>
  <footer>Fabrika v0 · reklam fazında harcama/CPI/ROAS panelleri eklenecek</footer>
"""

out_arg = sys.argv[1] if len(sys.argv) > 1 else "/var/www/fabrika/index.html"
out_dir = out_arg if os.path.isdir(out_arg) or not out_arg.endswith(".html") \
    else os.path.dirname(out_arg)
os.makedirs(out_dir, exist_ok=True)

written = []
p = os.path.join(out_dir, "index.html")
open(p, "w").write(page("Mobil Fabrika", index_body))
written.append(p)

# --- 2) DETAY SAYFALARI --------------------------------------------------------
for a in app_cards:
    body = f"""
  <header>
    <h1><a class="back" href="index.html">← Fabrika</a> {a['name']}</h1>
    <span class="muted">son üretim: {GEN}</span>
    <form class="qa refresh" method="post" action="/{TOKEN}/api/refresh">
      <input type="hidden" name="app" value="{a['id']}">
      <button type="submit">⟳ yenile</button>
    </form>
  </header>
  {a['detail']}
"""
    p = os.path.join(out_dir, f"{a['id']}.html")
    open(p, "w").write(page(f"{a['name']} — Fabrika", body,
                            {a["id"]: data_js.get(a["id"], {})}))
    written.append(p)

print(f"[panel] {len(written)} sayfa yazıldı → {out_dir}")
