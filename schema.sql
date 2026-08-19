-- Mobil Fabrika — veri omurgası (v0)
-- Uygulanacağı yer: DO Managed PG (ekap-pro'nun kullandığı küme), ayrı şema.
-- Tasarım: docs/superpowers/specs/2026-08-18-mobil-fabrika-beyni-design.md (M0)
--
-- v0 bilinçli olarak dar: apps + günlük metrik. Metrikler key-value tutulur ki
-- yeni bir metrik eklemek migration istemesin; kolonlu tablolara (creatives,
-- experiments, decisions) reklam fazında (Faz C/D) geçilir.

CREATE SCHEMA IF NOT EXISTS factory;

CREATE TABLE IF NOT EXISTS factory.apps (
  id         text PRIMARY KEY,          -- kısa slug: 'parentlaw'
  name       text NOT NULL,
  -- yaşam döngüsü: idea|build|soak|spark|scale|harvest|killed (playbook M5)
  status     text NOT NULL DEFAULT 'soak',
  notes      text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS factory.app_metrics_daily (
  app_id       text NOT NULL REFERENCES factory.apps(id),
  date         date NOT NULL,
  metric       text NOT NULL,           -- örn: users_total, subs_active, trial_starts
  segment      text NOT NULL DEFAULT '',-- örn: plan=family, platform=ios; boş = toplam
  value        numeric NOT NULL,
  collected_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (app_id, date, metric, segment)
);

INSERT INTO factory.apps (id, name, status, notes)
VALUES ('parentlaw', 'ParentLaw (Co-Parent)', 'soak', 'Pilot uygulama — ABD co-parenting')
ON CONFLICT (id) DO NOTHING;
