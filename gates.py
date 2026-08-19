#!/usr/bin/env python3
"""Kapı ve kota motoru — fabrikanın frenleri.

Her uygulamanın künyesindeki eşikleri günün verisiyle karşılaştırır ve tek bir
karar üretir: CONTINUE | SCALE | HOLD | KILL. Karar `factory.decisions`
tablosuna yazılır; panel ve (Faz C'de) executor buradan okur.

Tasarım kararları:
  * Örneklem disiplini: min_sample altındaki hiçbir metrik "başarısız" sayılmaz.
    Az veriyle uygulama öldürmek, fabrikanın yapabileceği en pahalı hata.
  * Harcama tavanı tek başına öldürmez; tavan DOLDU **ve** hiçbir kıvılcım
    metriği tutmadıysa öldürür. Kıvılcım varsa karar insana kalır (HOLD).
  * Motor hiçbir şeyi uygulamaz, yalnız karar yazar. Uygulama Faz C'de,
    onay kapısının arkasında.

Kullanım:  python3 gates.py [app_id ...]     (boşsa tüm aktif uygulamalar)
"""
import json, os, subprocess, sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
# Repoda scripts/factory/ altındayız → künyeler ../../factory/apps.
# Sunucuda düz kurulum (/opt/factory) → FACTORY_APPS_DIR ile göster.
APPS_DIR = os.environ.get("FACTORY_APPS_DIR") or os.path.join(
    HERE, "..", "..", "factory", "apps")
if not os.path.isdir(APPS_DIR) and os.path.isdir(os.path.join(HERE, "apps")):
    APPS_DIR = os.path.join(HERE, "apps")

TERMINAL = {"killed", "harvest"}


def load_env(path="/root/.factory.env"):
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v)


def q(sql):
    out = subprocess.run(["psql", os.environ["FACTORY_DATABASE_URL"], "-At", "-F", "\t", "-c", sql],
                         capture_output=True, text=True)
    if out.returncode:
        raise SystemExit(f"psql hatası: {out.stderr[:300]}")
    return [r.split("\t") for r in out.stdout.strip().split("\n") if r]


def yaml_lite(path):
    """Künye okuyucu — iç içe iki seviye + liste yeter, PyYAML bağımlılığı yok.
    ponytail: kendi şemamızı kendimiz yazıyoruz; tam YAML gerekmiyor."""
    root = {}
    stack = [(-1, root)]
    for raw in open(path):
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        body = line.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if body.startswith("- "):
            parent.setdefault("_list", []).append(body[2:].strip())
            continue
        if ":" not in body:
            continue
        key, val = body.split(":", 1)
        key, val = key.strip(), val.split("#")[0].strip()
        if not val:
            node = {}
            parent[key] = node
            stack.append((indent, node))
        elif val.startswith("{"):
            parent[key] = json.loads(val.replace("'", '"')
                                     .replace("{", '{"').replace(": ", '": ')
                                     .replace(", ", ', "'))
        elif val.startswith("["):
            parent[key] = [v.strip() for v in val.strip("[]").split(",") if v.strip()]
        else:
            try:
                parent[key] = float(val) if "." in val else int(val)
            except ValueError:
                parent[key] = val.strip('"')
    # listeleri düzelt: {_list: [...]} → [...]
    def fix(n):
        if isinstance(n, dict):
            if set(n) == {"_list"}:
                return n["_list"]
            return {k: fix(v) for k, v in n.items()}
        return n
    return fix(root)


def live_status(app_id, fallback):
    """Aşamanın sahibi DB'dir — panelden değiştirilebilir. Künyedeki değer
    yalnız DB'de kayıt yoksa (ya da DB erişilemezse) yedektir."""
    try:
        rows = q(f"SELECT status FROM factory.apps WHERE id='{app_id}'")
        return rows[0][0] if rows and rows[0][0] else fallback
    except Exception:
        return fallback


def metrics_for(app_id, days=14):
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = q(f"""SELECT date::text, metric, segment, value FROM factory.app_metrics_daily
                 WHERE app_id='{app_id}' AND date >= '{since}' ORDER BY date""")
    return rows


def total(rows, metric, segment=None):
    return sum(float(r[3]) for r in rows
               if r[1] == metric and (segment is None or r[2] == segment))


def evaluate(cfg, rows):
    """Künye + veri → (karar, gerekçeler, kanıt)."""
    gid = cfg["id"]
    status = live_status(gid, cfg.get("status", "soak"))
    budget = cfg.get("budget", {})
    gates = cfg.get("gates", {})
    minsmp = cfg.get("min_sample", {})

    installs = total(rows, "store_downloads", "ios")
    spend = total(rows, "ad_spend_usd")            # reklam borusu gelince dolar
    trials = total(rows, "trial_starts")
    paid = total(rows, "subs_new")
    revenue = total(rows, "store_proceeds_usd", "ios")

    ev = {"installs": installs, "spend": spend, "trials": trials,
          "paid": paid, "revenue": revenue}
    why = []

    # --- örneklem kapısı: veri yetersizse asla olumsuz karar verme ---
    if installs < minsmp.get("installs", 100):
        why.append(f"Örneklem yetersiz: {int(installs)} indirme "
                   f"(eşik {minsmp.get('installs', 100)}). Kapılar değerlendirilmedi.")
        return "CONTINUE", why, ev

    spark = gates.get("spark", {})
    trial_rate = trials / installs if installs else 0
    t2p = paid / trials if trials else 0

    hits, misses = [], []
    for name, actual, target in [
        ("trial başlatma", trial_rate, spark.get("trial_start_rate", 0.05)),
        ("trial→paid", t2p, spark.get("trial_to_paid", 0.35)),
    ]:
        (hits if actual >= target else misses).append(
            f"{name} %{actual*100:.1f} (hedef %{target*100:.0f})")

    ltv_cac = (revenue / spend) if spend > 0 else None
    if ltv_cac is not None:
        target = spark.get("ltv_cac", 1.0)
        (hits if ltv_cac >= target else misses).append(
            f"LTV:CAC {ltv_cac:.2f} (hedef {target:.2f})")

    cap = budget.get("spark_total_cap_usd", 0)
    over_cap = cap and spend >= cap

    # --- karar ---
    if over_cap and not hits:
        why.append(f"Test bütçesi doldu (${spend:.0f}/${cap:.0f}) ve hiçbir kıvılcım "
                   f"metriği tutmadı: {', '.join(misses)}.")
        return "KILL", why, ev
    if over_cap and hits:
        why.append(f"Bütçe tavanı doldu (${spend:.0f}/${cap:.0f}) ama kıvılcım var: "
                   f"{', '.join(hits)}. Tavan artışı insan kararı.")
        return "HOLD", why, ev

    scale = gates.get("scale", {})
    if ltv_cac is not None and ltv_cac >= scale.get("ltv_cac", 1.2) and status in ("spark", "scale"):
        why.append(f"LTV:CAC {ltv_cac:.2f} ölçek eşiğinin üstünde → "
                   f"bütçe +%{budget.get('scale_step_pct', 25)} önerilir.")
        return "SCALE", why, ev

    if hits:
        why.append("Tutan metrikler: " + ", ".join(hits))
    if misses:
        why.append("Tutmayan metrikler: " + ", ".join(misses))
    if spend:
        why.append(f"Harcama ${spend:.0f}/${cap:.0f}.")
    return "CONTINUE", why, ev


def main():
    load_env()
    load_env("/root/.asc.env")
    wanted = sys.argv[1:]
    files = [f for f in sorted(os.listdir(APPS_DIR)) if f.endswith((".yaml", ".yml"))]
    out = []
    for f in files:
        cfg = yaml_lite(os.path.join(APPS_DIR, f))
        if wanted and cfg["id"] not in wanted:
            continue
        if cfg.get("status") in TERMINAL:
            continue
        # Bütçe tavanlarının sahibi kullanıcı: panelden girilen değer künyeyi
        # geçersiz kılar. evaluate() saf kalsın diye birleştirme burada.
        try:
            for k, v in q(f"""SELECT key, value FROM factory.app_inputs
                              WHERE app_id='{cfg["id"]}'
                              AND key IN ('budget_daily_usd','budget_spark_cap_usd')"""):
                hedef = {"budget_daily_usd": "daily_cap_usd",
                         "budget_spark_cap_usd": "spark_total_cap_usd"}[k]
                cfg.setdefault("budget", {})[hedef] = float(v)
        except Exception:
            pass  # DB'ye ulaşılamazsa künye değerleri geçerli kalır
        rows = metrics_for(cfg["id"])
        decision, why, ev = evaluate(cfg, rows)
        out.append({"app": cfg["id"], "decision": decision, "why": why, "evidence": ev})
        print(f"[{cfg['id']}] {decision}")
        for w in why:
            print("   -", w)

    # kararları DB'ye yaz (panel + beyin okur)
    if out:
        sql = ["BEGIN;"]
        for o in out:
            payload = json.dumps(o, ensure_ascii=False).replace("'", "''")
            sql.append(
                "INSERT INTO factory.app_metrics_daily (app_id, date, metric, segment, value) "
                f"VALUES ('{o['app']}', current_date, 'gate_decision', "
                f"'{o['decision']}', 1) "
                "ON CONFLICT (app_id, date, metric, segment) "
                "DO UPDATE SET value = EXCLUDED.value, collected_at = now();")
            sql.append(
                "INSERT INTO factory.decisions (app_id, kind, payload) "
                f"VALUES ('{o['app']}', 'gate', '{payload}'::jsonb);")
        sql.append("COMMIT;")
        p = subprocess.run(["psql", os.environ["FACTORY_DATABASE_URL"], "-q"],
                           input="\n".join(sql), capture_output=True, text=True)
        if p.returncode:
            print("DB yazım hatası:", p.stderr[:300], file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
