#!/usr/bin/env python3
"""Rakip ASO istihbaratı — bugün elle yapılan analizin aracı hâli.

Künyedeki çekirdek aramalarda kimin çıktığını, uygulamamızın çıkıp çıkmadığını
ve kategorinin gerçek kelime dağarcığını çıkarır. Çıktı:
factory/intel/{app}.json — ASO paketi ve kreatif brief'i bunu okur.

    python3 aso_research.py parentlaw
"""
import collections, json, os, re, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from gates import yaml_lite  # noqa: E402

from gates import APPS_DIR  # tek kaynak: gates.py yolu çözer
OUT_DIR = os.environ.get("FACTORY_INTEL_DIR") or os.path.join(HERE, "intel")

# Açıklama metinlerinde her uygulamada geçen ama arama değeri olmayan kelimeler.
STOP = set("""the a an and or of to for in with your you can that this it is are on all we our
their they be from at as by not have has if so more than when who will do does app apps get
new now out use using more your all one two per any may""".split())


def search(term, country="us", limit=10):
    u = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": term, "country": country, "entity": "software", "limit": limit})
    try:
        return json.load(urllib.request.urlopen(u, timeout=25)).get("results", [])
    except Exception as e:
        print(f"  ! '{term}' araması başarısız: {e}", file=sys.stderr)
        return []


def main(app_id):
    cfg = yaml_lite(os.path.join(APPS_DIR, app_id + ".yaml"))
    market = cfg.get("market", {})
    searches = market.get("core_searches", [])
    country = market.get("primary", "US").lower()
    our_bundle = cfg.get("bundle_id", "")

    rankings, pool = {}, {}
    print(f"🔎 {cfg['name']} — {len(searches)} çekirdek arama ({country.upper()})")
    for term in searches:
        res = search(term, country)
        ours = None
        row = []
        for i, r in enumerate(res, 1):
            row.append({"pos": i, "name": r.get("trackName"),
                        "bundle": r.get("bundleId"),
                        "rating": r.get("averageUserRating"),
                        "ratings": r.get("userRatingCount")})
            if r.get("bundleId") == our_bundle:
                ours = i
            pool[r.get("trackId")] = r
        rankings[term] = {"our_position": ours, "top": row[:8]}
        print(f"  · {term:<24} bizim sıra: {ours if ours else 'YOK ❌'}")
        time.sleep(0.4)

    # kategori dili — yalnız gerçek oyunculardan (100+ yorum) öğren
    players = [c for c in pool.values() if (c.get("userRatingCount") or 0) >= 100]
    players.sort(key=lambda c: -(c.get("userRatingCount") or 0))
    words, bigrams = collections.Counter(), collections.Counter()
    for c in players:
        txt = re.sub(r"[^a-z\s]", " ", (c.get("trackName", "") + " " +
                                        c.get("description", "")).lower())
        ws = [w for w in txt.split() if len(w) > 2 and w not in STOP]
        words.update(ws)
        bigrams.update(f"{ws[i]} {ws[i+1]}" for i in range(len(ws) - 1))

    out = {
        "app": app_id,
        "generated": datetime.now(timezone.utc).isoformat(),
        "market": country.upper(),
        "rankings": rankings,
        "invisible_searches": [t for t, v in rankings.items() if v["our_position"] is None],
        "competitors": [
            {"name": c.get("trackName"), "bundle": c.get("bundleId"),
             "ratings": c.get("userRatingCount"), "rating": c.get("averageUserRating"),
             "price": c.get("formattedPrice"),
             "title_pattern": c.get("trackName"),
             "hook": (c.get("description") or "")[:220]}
            for c in players[:10]],
        "vocabulary": {
            "words": [{"w": w, "n": n} for w, n in words.most_common(30)],
            "phrases": [{"p": p, "n": n} for p, n in bigrams.most_common(25)
                        if not p.startswith(("https", "www", "com ", "terms"))][:20],
        },
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{app_id}.json")
    json.dump(out, open(path, "w"), ensure_ascii=False, indent=1)

    # Sıralamayı factory DB'ye de yaz: ASO işinin sonucu ancak TREND olarak
    # ölçülür. 999 = "ilk 10'da yok" (grafikte düşen çizgi iyileşme demektir).
    if os.environ.get("FACTORY_DATABASE_URL"):
        import subprocess
        from datetime import date
        today = date.today().isoformat()
        sql = ["BEGIN;"]
        visible = 0
        for term, v in rankings.items():
            pos = v["our_position"]
            seg = term.replace("'", "")[:60]
            sql.append(
                "INSERT INTO factory.app_metrics_daily (app_id,date,metric,segment,value) "
                f"VALUES ('{app_id}','{today}','aso_rank','term={seg}',{pos or 999}) "
                "ON CONFLICT (app_id,date,metric,segment) DO UPDATE SET "
                "value=EXCLUDED.value, collected_at=now();")
            if pos:
                visible += 1
        sql.append(
            "INSERT INTO factory.app_metrics_daily (app_id,date,metric,segment,value) "
            f"VALUES ('{app_id}','{today}','aso_visible_searches','',{visible}) "
            "ON CONFLICT (app_id,date,metric,segment) DO UPDATE SET "
            "value=EXCLUDED.value, collected_at=now();")
        sql.append("COMMIT;")
        subprocess.run(["psql", os.environ["FACTORY_DATABASE_URL"], "-q"],
                       input="\n".join(sql), capture_output=True, text=True)

    inv = out["invisible_searches"]
    print(f"\n📊 {len(players)} gerçek rakip çözümlendi → {path}")
    if inv:
        print(f"⚠️  görünmediğimiz aramalar ({len(inv)}/{len(searches)}): {', '.join(inv)}")
    else:
        print("✅ tüm çekirdek aramalarda görünüyoruz")
    print("   kategori dili:", ", ".join(w["w"] for w in out["vocabulary"]["words"][:12]))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "parentlaw")
