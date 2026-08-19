#!/usr/bin/env python3
"""Kapı motorunun karar mantığı için tek dosyalık kontrol.

Bu mantık uygulama öldürür ve bütçe artırır; sessizce bozulursa fabrika ya
kazananı keser ya kaybedene para akıtır. Çalıştır:  python3 test_gates.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gates import evaluate, yaml_lite, APPS_DIR

CFG = yaml_lite(os.path.join(APPS_DIR, "parentlaw.yaml"))


def rows(installs=0, spend=0, trials=0, paid=0, revenue=0):
    """metrics_for() satır biçimi: [tarih, metrik, segment, değer]"""
    out = []
    if installs: out.append(["2026-08-19", "store_downloads", "ios", installs])
    if spend:    out.append(["2026-08-19", "ad_spend_usd", "", spend])
    if trials:   out.append(["2026-08-19", "trial_starts", "", trials])
    if paid:     out.append(["2026-08-19", "subs_new", "", paid])
    if revenue:  out.append(["2026-08-19", "store_proceeds_usd", "ios", revenue])
    return out


def check(label, r, expected):
    got, why, _ = evaluate(CFG, r)
    ok = got == expected
    print(f"{'✓' if ok else '✗'} {label}: {got} (beklenen {expected})")
    if not ok:
        print("   gerekçeler:", why)
    assert ok, label


# 1) Az veriyle ASLA öldürme — fabrikanın en pahalı hatası budur.
check("örneklem yetersizken öldürmez",
      rows(installs=40, spend=3000, trials=0), "CONTINUE")

# 2) Tavan doldu + hiçbir metrik tutmadı → KILL
check("tavan doldu, kıvılcım yok → KILL",
      rows(installs=500, spend=2600, trials=5, paid=0, revenue=10), "KILL")

# 3) Tavan doldu AMA kıvılcım var → insana bırak (HOLD), otomatik öldürme
check("tavan doldu, kıvılcım var → HOLD",
      rows(installs=500, spend=2600, trials=60, paid=30, revenue=900), "HOLD")

# 4) Ekonomi güçlü, tavan dolmadı → SCALE (künye status=soak olduğu için
#    ölçek kararı yalnız spark/scale aşamasında verilir → CONTINUE beklenir)
check("soak aşamasında ölçek kararı verilmez",
      rows(installs=500, spend=500, trials=60, paid=30, revenue=900), "CONTINUE")

# 5) spark aşamasında güçlü ekonomi → SCALE
spark_cfg = dict(CFG, status="spark")
got, why, _ = evaluate(spark_cfg, rows(installs=500, spend=500, trials=60,
                                       paid=30, revenue=900))
print(f"{'✓' if got == 'SCALE' else '✗'} spark aşamasında güçlü ekonomi → {got}")
assert got == "SCALE", why

# 6) Harcama yokken (organik dönem) karar CONTINUE olmalı, bölme hatası olmamalı
check("harcama sıfırken çökmez",
      rows(installs=500, trials=30, paid=12), "CONTINUE")

print("\nTüm kontroller geçti.")
