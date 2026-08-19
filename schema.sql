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

-- Kreatif onay akışı (v1): üretilen her kreatif yayına insan onayıyla gider.
-- Ret/revizyon notları brief üreticisine geri beslenir (zevk öğrenme verisi).
CREATE TABLE IF NOT EXISTS factory.creatives (
  id             serial PRIMARY KEY,
  app_id         text NOT NULL REFERENCES factory.apps(id),
  brief_id       text NOT NULL,            -- intel ads.parentlaw_briefs[].id
  concept        text NOT NULL DEFAULT '', -- A/B/C konsept etiketi
  format         text NOT NULL DEFAULT '', -- statik-1x1 | statik-9x16 | video-30s ...
  file           text NOT NULL,            -- /var/www/fabrika/creatives/ altındaki dosya adı
  hook           text NOT NULL DEFAULT '',
  body           text NOT NULL DEFAULT '',
  cta            text NOT NULL DEFAULT '',
  cost_usd       numeric NOT NULL DEFAULT 0,
  source_pattern text NOT NULL DEFAULT '', -- hangi rakip deseninden türedi
  review_status  text NOT NULL DEFAULT 'pending'
                 CHECK (review_status IN ('pending','approved','rejected','revision_requested','revising')),
  review_note    text,
  reviewed_at    timestamptz,
  revision_of    integer REFERENCES factory.creatives(id),
  revision_count integer NOT NULL DEFAULT 0,
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS factory.review_events (
  id          serial PRIMARY KEY,
  creative_id integer NOT NULL REFERENCES factory.creatives(id),
  action      text NOT NULL,               -- approved | rejected | revision_requested
  note        text,
  created_at  timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE ON factory.creatives, factory.review_events TO factory_writer;
GRANT USAGE ON SEQUENCE factory.creatives_id_seq, factory.review_events_id_seq TO factory_writer;
