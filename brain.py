#!/usr/bin/env python3
"""Fabrika beyni — veriyi okur, playbook'a göre yorumlar, panele yazar.

`gates.py` deterministik kural motorudur (eşik aşıldı mı?). Bu ise YORUMLAYAN
katmandır: aynı veriye bakıp "ne oluyor, sırada ne var, neden" der ve panele
Beyin notu olarak düşer. Ops host'ta abonelik token'lı `claude -p` ile koşar
(faturasız); claude yoksa sessizce kural-bazlı özete düşer — panel asla boş
kalmaz.

    python3 brain.py [app_id ...]

Tasarım kararları:
  * Beyin YALNIZ yazar, hiçbir şey uygulamaz. Aksiyon Faz C'de onay kapısının
    arkasında (ops_actions deseni).
  * Panelden gelen kullanıcı istekleri (factory.messages) prompt'a girer —
    beyin ne istendiğini bilerek yorumlar.
  * Çıktı kısa tutulur: panelde 3-5 cümle okunur, sayfa dolusu analiz değil.
"""
import json, os, subprocess, sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from gates import load_env, q, yaml_lite, APPS_DIR  # noqa: E402

PLAYBOOK = os.environ.get("FACTORY_PLAYBOOK") or os.path.join(
    HERE, "..", "..", "factory", "playbooks", "core.md")
MODEL = os.environ.get("BRAIN_MODEL", "claude-opus-4-8")


def snapshot(app_id):
    """Beyne verilecek veri özeti — ham satır değil, okunur tablo."""
    since = (date.today() - timedelta(days=21)).isoformat()
    rows = q(f"""SELECT date::text, metric, segment, value FROM factory.app_metrics_daily
                 WHERE app_id='{app_id}' AND date >= '{since}'
                 ORDER BY date DESC, metric LIMIT 200""")
    stages = q(f"""SELECT key, ok, note FROM factory.app_status
                   WHERE app_id='{app_id}' AND kind='stage' ORDER BY ord""")
    intake = q(f"""SELECT label, ok FROM factory.app_status
                   WHERE app_id='{app_id}' AND kind='intake' ORDER BY ord""")
    gate = q(f"""SELECT payload::text FROM factory.decisions
                 WHERE app_id='{app_id}' AND kind='gate'
                 ORDER BY created_at DESC LIMIT 1""")
    msgs = q(f"""SELECT body FROM factory.messages
                 WHERE app_id='{app_id}' AND direction='user' AND status='new'
                 ORDER BY created_at DESC LIMIT 5""")
    return {
        "metrics": [f"{r[0]} {r[1]}{('/' + r[2]) if r[2] else ''} = {r[3]}" for r in rows[:60]],
        "stages": [f"{'TAMAM' if r[1] == 't' else 'EKSİK'} {r[0]}: {r[2]}" for r in stages],
        "intake_missing": [r[0] for r in intake if r[1] != "t"],
        "gate": gate[0][0] if gate else "",
        "user_requests": [r[0] for r in msgs],
    }


PROMPT = """Sen bir mobil uygulama fabrikasının analiz beynisin. Aşağıdaki
playbook kurallarına ve GERÇEK veriye bakıp kısa bir durum yorumu yaz.

KURALLAR:
- Türkçe yaz, 3-5 cümle, madde işareti kullanma.
- Veriyi tekrar etme; ANLAMINI söyle ve sıradaki tek en önemli adımı belirt.
- Elinde olmayan veriyi varmış gibi konuşma; eksikse "ölçülemiyor" de.
- Abartma ve övgü yok. Kötü haberi doğrudan söyle.
- Kullanıcı istekleri varsa onlara açıkça değin.

PLAYBOOK:
{playbook}

UYGULAMA: {name} (durum: {status})

BORU HATTI:
{stages}

EKSİK İNTAKE: {intake}

SON KAPI KARARI: {gate}

PANELDEN GELEN İSTEKLER: {requests}

SON 21 GÜN VERİSİ:
{metrics}
"""


def ask_claude(prompt):
    """claude -p ile yorum al. Yoksa None döner (çağıran yedeğe düşer)."""
    exe = subprocess.run(["which", "claude"], capture_output=True, text=True)
    if exe.returncode != 0:
        return None
    try:
        r = subprocess.run(["claude", "-p", "--model", MODEL],
                           input=prompt, capture_output=True, text=True, timeout=240)
        out = (r.stdout or "").strip()
        return out if out else None
    except Exception:
        return None


def fallback(snap, cfg):
    """claude yokken kural-bazlı özet — panel asla boş kalmasın."""
    parts = []
    eksik = [s for s in snap["stages"] if s.startswith("EKSİK")]
    if eksik:
        parts.append(f"Boru hattında {len(eksik)} aşama eksik; ilki: "
                     + eksik[0].split(':')[0].replace('EKSİK ', '') + ".")
    if snap["intake_missing"]:
        parts.append("Bekleyen intake: " + ", ".join(snap["intake_missing"]) + ".")
    if snap["user_requests"]:
        parts.append(f"Panelden {len(snap['user_requests'])} açık istek var.")
    if not snap["metrics"]:
        parts.append("Son 21 günde veri yok — toplayıcıları kontrol et.")
    return " ".join(parts) or "Durum sakin; yeni sinyal yok."


def main():
    load_env()
    load_env("/root/.google.env")
    wanted = sys.argv[1:]
    playbook = open(PLAYBOOK).read()[:4000] if os.path.exists(PLAYBOOK) else ""

    for f in sorted(os.listdir(APPS_DIR)):
        if not f.endswith((".yaml", ".yml")):
            continue
        cfg = yaml_lite(os.path.join(APPS_DIR, f))
        if wanted and cfg["id"] not in wanted:
            continue
        if cfg.get("status") in ("killed", "harvest"):
            continue

        snap = snapshot(cfg["id"])
        prompt = PROMPT.format(
            playbook=playbook, name=cfg["name"], status=cfg.get("status"),
            stages="\n".join(snap["stages"]) or "(yayınlanmadı)",
            intake=", ".join(snap["intake_missing"]) or "yok",
            gate=snap["gate"][:400] or "(karar yok)",
            requests="\n".join(snap["user_requests"]) or "(yok)",
            metrics="\n".join(snap["metrics"]) or "(veri yok)")

        note = ask_claude(prompt)
        source = "claude" if note else "kural"
        if not note:
            note = fallback(snap, cfg)

        esc = note.replace("'", "''")[:2000]
        subprocess.run(["psql", os.environ["FACTORY_DATABASE_URL"], "-q", "-c",
                        "INSERT INTO factory.messages (app_id,direction,body,status) "
                        f"VALUES ('{cfg['id']}','brain','{esc}','seen');"],
                       capture_output=True)
        print(f"[beyin/{source}] {cfg['id']}: {note[:160]}")


if __name__ == "__main__":
    main()
