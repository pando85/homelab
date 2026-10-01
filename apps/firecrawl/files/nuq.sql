-- Source: https://raw.githubusercontent.com/firecrawl/firecrawl/v2.10.19/apps/nuq-postgres/nuq.sql
-- Git ref: v2.10.19
-- Adapted: 2026-10-02 for idempotent re-runs via init container
-- Changes: removed ALTER SYSTEM statements (set via Zalando CR instead),
--          removed SELECT pg_reload_conf(), kept pg_cron jobs (pg_cron available),
--          CREATE TYPE already guarded upstream with DO $$ / EXCEPTION blocks.

-- Serialize concurrent init-container runs (api/worker/nuq-worker roll together)
SELECT pg_advisory_lock(74920193);

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_cron;

CREATE SCHEMA IF NOT EXISTS nuq;

DO $$ BEGIN
  CREATE TYPE nuq.job_status AS ENUM ('queued', 'active', 'completed', 'failed');
EXCEPTION
  WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
  CREATE TYPE nuq.group_status AS ENUM ('active', 'completed', 'cancelled');
EXCEPTION
  WHEN duplicate_object THEN null;
END $$;

CREATE TABLE IF NOT EXISTS nuq.queue_scrape (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  status nuq.job_status NOT NULL DEFAULT 'queued'::nuq.job_status,
  data jsonb,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  priority int NOT NULL DEFAULT 0,
  lock uuid,
  locked_at timestamp with time zone,
  stalls integer,
  finished_at timestamp with time zone,
  listen_channel_id text,
  returnvalue jsonb,
  failedreason text,
  owner_id uuid,
  group_id uuid,
  CONSTRAINT queue_scrape_pkey PRIMARY KEY (id)
);

ALTER TABLE nuq.queue_scrape
SET (autovacuum_vacuum_scale_factor = 0.01,
     autovacuum_analyze_scale_factor = 0.01,
     autovacuum_vacuum_cost_limit = 10000,
     autovacuum_vacuum_cost_delay = 0);

CREATE INDEX IF NOT EXISTS queue_scrape_active_locked_at_idx ON nuq.queue_scrape USING btree (locked_at) WHERE (status = 'active'::nuq.job_status);
CREATE INDEX IF NOT EXISTS nuq_queue_scrape_queued_optimal_2_idx ON nuq.queue_scrape (priority ASC, created_at ASC, id) WHERE (status = 'queued'::nuq.job_status);
CREATE INDEX IF NOT EXISTS nuq_queue_scrape_failed_created_at_idx ON nuq.queue_scrape USING btree (created_at) WHERE (status = 'failed'::nuq.job_status);
CREATE INDEX IF NOT EXISTS nuq_queue_scrape_completed_standalone_created_at_idx ON nuq.queue_scrape USING btree (created_at) WHERE (status = 'completed'::nuq.job_status AND group_id IS NULL);
CREATE INDEX IF NOT EXISTS nuq_queue_scrape_failed_standalone_created_at_idx ON nuq.queue_scrape USING btree (created_at) WHERE (status = 'failed'::nuq.job_status AND group_id IS NULL);
CREATE INDEX IF NOT EXISTS nuq_queue_scrape_group_id_idx ON nuq.queue_scrape (group_id) WHERE group_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS nuq_queue_scrape_group_owner_mode_idx ON nuq.queue_scrape (group_id, owner_id) WHERE ((data->>'mode') = 'single_urls');
CREATE INDEX IF NOT EXISTS nuq_queue_scrape_group_mode_status_idx ON nuq.queue_scrape (group_id, status) WHERE ((data->>'mode') = 'single_urls');
CREATE INDEX IF NOT EXISTS nuq_queue_scrape_group_completed_listing_idx ON nuq.queue_scrape (group_id, finished_at ASC, created_at ASC) WHERE (status = 'completed'::nuq.job_status AND (data->>'mode') = 'single_urls');
CREATE INDEX IF NOT EXISTS idx_queue_scrape_group_status ON nuq.queue_scrape (group_id, status) WHERE status IN ('active', 'queued');

CREATE TABLE IF NOT EXISTS nuq.queue_scrape_backlog (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  data jsonb,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  priority int NOT NULL DEFAULT 0,
  listen_channel_id text,
  owner_id uuid,
  group_id uuid,
  times_out_at timestamptz,
  CONSTRAINT queue_scrape_backlog_pkey PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS nuq_queue_scrape_backlog_owner_id_idx ON nuq.queue_scrape_backlog (owner_id);
CREATE INDEX IF NOT EXISTS nuq_queue_scrape_backlog_group_mode_idx ON nuq.queue_scrape_backlog (group_id) WHERE ((data->>'mode') = 'single_urls');
CREATE INDEX IF NOT EXISTS nuq_queue_scrape_backlog_times_out_at_idx ON nuq.queue_scrape_backlog (times_out_at);

SELECT cron.schedule('nuq_queue_scrape_clean_completed', '*/5 * * * *', $$
  DELETE FROM nuq.queue_scrape WHERE nuq.queue_scrape.status = 'completed'::nuq.job_status AND nuq.queue_scrape.created_at < now() - interval '1 hour' AND group_id IS NULL;
$$);

SELECT cron.schedule('nuq_queue_scrape_clean_failed', '*/5 * * * *', $$
  DELETE FROM nuq.queue_scrape WHERE nuq.queue_scrape.status = 'failed'::nuq.job_status AND nuq.queue_scrape.created_at < now() - interval '6 hours' AND group_id IS NULL;
$$);

SELECT cron.schedule('nuq_queue_scrape_lock_reaper', '15 seconds', $$
  UPDATE nuq.queue_scrape SET status = 'queued'::nuq.job_status, lock = null, locked_at = null, stalls = COALESCE(stalls, 0) + 1 WHERE nuq.queue_scrape.locked_at <= now() - interval '1 minute' AND nuq.queue_scrape.status = 'active'::nuq.job_status AND COALESCE(nuq.queue_scrape.stalls, 0) < 9;
  WITH stallfail AS (UPDATE nuq.queue_scrape SET status = 'failed'::nuq.job_status, lock = null, locked_at = null, stalls = COALESCE(stalls, 0) + 1 WHERE nuq.queue_scrape.locked_at <= now() - interval '1 minute' AND nuq.queue_scrape.status = 'active'::nuq.job_status AND COALESCE(nuq.queue_scrape.stalls, 0) >= 9 RETURNING id)
  SELECT pg_notify('nuq.queue_scrape', (id::text || '|' || 'failed'::text)) FROM stallfail;
$$);

SELECT cron.schedule('nuq_queue_scrape_backlog_reaper', '* * * * *', $$
  SET statement_timeout = '50s';
  DELETE FROM nuq.queue_scrape_backlog
  WHERE nuq.queue_scrape_backlog.times_out_at < now();
$$);

SELECT cron.schedule('nuq_reindex_queue_scrape_pkey',                   '0 2 * * *',  $$REINDEX INDEX CONCURRENTLY nuq.queue_scrape_pkey;$$);
SELECT cron.schedule('nuq_reindex_queue_scrape_active_locked_at',       '20 2 * * *', $$REINDEX INDEX CONCURRENTLY nuq.queue_scrape_active_locked_at_idx;$$);
SELECT cron.schedule('nuq_reindex_queue_scrape_queued_optimal_2',       '40 2 * * *', $$REINDEX INDEX CONCURRENTLY nuq.nuq_queue_scrape_queued_optimal_2_idx;$$);
SELECT cron.schedule('nuq_reindex_queue_scrape_failed_created_at',      '0 3 * * *',  $$REINDEX INDEX CONCURRENTLY nuq.nuq_queue_scrape_failed_created_at_idx;$$);
SELECT cron.schedule('nuq_reindex_queue_scrape_group_owner_mode',       '40 3 * * *', $$REINDEX INDEX CONCURRENTLY nuq.nuq_queue_scrape_group_owner_mode_idx;$$);
SELECT cron.schedule('nuq_reindex_queue_scrape_group_mode_status',      '0 4 * * *',  $$REINDEX INDEX CONCURRENTLY nuq.nuq_queue_scrape_group_mode_status_idx;$$);
SELECT cron.schedule('nuq_reindex_queue_scrape_group_completed_listing','20 4 * * *', $$REINDEX INDEX CONCURRENTLY nuq.nuq_queue_scrape_group_completed_listing_idx;$$);
SELECT cron.schedule('nuq_reindex_queue_scrape_group_status',           '40 4 * * *', $$REINDEX INDEX CONCURRENTLY nuq.idx_queue_scrape_group_status;$$);

SELECT cron.schedule('nuq_reindex_queue_scrape_backlog_pkey',           '0 5 * * *',  $$REINDEX INDEX CONCURRENTLY nuq.queue_scrape_backlog_pkey;$$);
SELECT cron.schedule('nuq_reindex_queue_scrape_backlog_owner_id',       '20 5 * * *', $$REINDEX INDEX CONCURRENTLY nuq.nuq_queue_scrape_backlog_owner_id_idx;$$);
SELECT cron.schedule('nuq_reindex_queue_scrape_backlog_group_mode',     '40 5 * * *', $$REINDEX INDEX CONCURRENTLY nuq.nuq_queue_scrape_backlog_group_mode_idx;$$);
SELECT cron.schedule('nuq_reindex_queue_scrape_backlog_group_id',       '0 6 * * *',  $$REINDEX INDEX CONCURRENTLY nuq.idx_queue_scrape_backlog_group_id;$$);
SELECT cron.schedule('nuq_reindex_queue_scrape_backlog_times_out_at',   '20 6 * * *', $$REINDEX INDEX CONCURRENTLY nuq.nuq_queue_scrape_backlog_times_out_at_idx;$$);

SELECT cron.schedule('nuq_reindex_queue_scrape_completed_standalone',   '40 6 * * *', $$REINDEX INDEX CONCURRENTLY nuq.nuq_queue_scrape_completed_standalone_created_at_idx;$$);
SELECT cron.schedule('nuq_reindex_queue_scrape_failed_standalone',      '40 8 * * *', $$REINDEX INDEX CONCURRENTLY nuq.nuq_queue_scrape_failed_standalone_created_at_idx;$$);
SELECT cron.schedule('nuq_reindex_queue_scrape_group_id',               '40 9 * * *', $$REINDEX INDEX CONCURRENTLY nuq.nuq_queue_scrape_group_id_idx;$$);

SELECT cron.schedule('nuq_maintenance_watchdog', '* * * * *', $$
  SELECT pg_cancel_backend(pid)
  FROM pg_stat_activity
  WHERE backend_type = 'client backend'
    AND query ILIKE 'REINDEX INDEX CONCURRENTLY nuq.%'
    AND now() - query_start > interval '18 minutes';
$$);

CREATE TABLE IF NOT EXISTS nuq.queue_crawl_finished (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  status nuq.job_status NOT NULL DEFAULT 'queued'::nuq.job_status,
  data jsonb,
  created_at timestamp with time zone NOT NULL DEFAULT now(),
  priority int NOT NULL DEFAULT 0,
  lock uuid,
  locked_at timestamp with time zone,
  stalls integer,
  finished_at timestamp with time zone,
  listen_channel_id text,
  returnvalue jsonb,
  failedreason text,
  owner_id uuid,
  group_id uuid,
  CONSTRAINT queue_crawl_finished_pkey PRIMARY KEY (id)
);

ALTER TABLE nuq.queue_crawl_finished
SET (autovacuum_vacuum_scale_factor = 0.01,
     autovacuum_analyze_scale_factor = 0.01,
     autovacuum_vacuum_cost_limit = 10000,
     autovacuum_vacuum_cost_delay = 0);

CREATE INDEX IF NOT EXISTS queue_crawl_finished_active_locked_at_idx ON nuq.queue_crawl_finished USING btree (locked_at) WHERE (status = 'active'::nuq.job_status);
CREATE INDEX IF NOT EXISTS nuq_queue_crawl_finished_queued_optimal_2_idx ON nuq.queue_crawl_finished (priority ASC, created_at ASC, id) WHERE (status = 'queued'::nuq.job_status);
CREATE INDEX IF NOT EXISTS nuq_queue_crawl_finished_failed_created_at_idx ON nuq.queue_crawl_finished USING btree (created_at) WHERE (status = 'failed'::nuq.job_status);
CREATE INDEX IF NOT EXISTS nuq_queue_crawl_finished_completed_created_at_idx ON nuq.queue_crawl_finished USING btree (created_at) WHERE (status = 'completed'::nuq.job_status);
CREATE INDEX IF NOT EXISTS nuq_queue_crawl_finished_completed_standalone_created_at_idx ON nuq.queue_crawl_finished USING btree (created_at) WHERE (status = 'completed'::nuq.job_status AND group_id IS NULL);
CREATE INDEX IF NOT EXISTS nuq_queue_crawl_finished_failed_standalone_created_at_idx ON nuq.queue_crawl_finished USING btree (created_at) WHERE (status = 'failed'::nuq.job_status AND group_id IS NULL);
CREATE INDEX IF NOT EXISTS nuq_queue_crawl_finished_group_id_idx ON nuq.queue_crawl_finished (group_id) WHERE group_id IS NOT NULL;

SELECT cron.schedule('nuq_queue_crawl_finished_clean_completed', '*/5 * * * *', $$
  DELETE FROM nuq.queue_crawl_finished WHERE nuq.queue_crawl_finished.status = 'completed'::nuq.job_status AND nuq.queue_crawl_finished.created_at < now() - interval '1 hour' AND group_id IS NULL;
$$);

SELECT cron.schedule('nuq_queue_crawl_finished_clean_failed', '*/5 * * * *', $$
  DELETE FROM nuq.queue_crawl_finished WHERE nuq.queue_crawl_finished.status = 'failed'::nuq.job_status AND nuq.queue_crawl_finished.created_at < now() - interval '6 hours' AND group_id IS NULL;
$$);

SELECT cron.schedule('nuq_queue_crawl_finished_lock_reaper', '15 seconds', $$
  UPDATE nuq.queue_crawl_finished SET status = 'queued'::nuq.job_status, lock = null, locked_at = null, stalls = COALESCE(stalls, 0) + 1 WHERE nuq.queue_crawl_finished.locked_at <= now() - interval '1 minute' AND nuq.queue_crawl_finished.status = 'active'::nuq.job_status AND COALESCE(nuq.queue_crawl_finished.stalls, 0) < 9;
  WITH stallfail AS (UPDATE nuq.queue_crawl_finished SET status = 'failed'::nuq.job_status, lock = null, locked_at = null, stalls = COALESCE(stalls, 0) + 1 WHERE nuq.queue_crawl_finished.locked_at <= now() - interval '1 minute' AND nuq.queue_crawl_finished.status = 'active'::nuq.job_status AND COALESCE(nuq.queue_crawl_finished.stalls, 0) >= 9 RETURNING id)
  SELECT pg_notify('nuq.queue_crawl_finished', (id::text || '|' || 'failed'::text)) FROM stallfail;
$$);

SELECT cron.schedule('nuq_reindex_queue_crawl_finished_pkey',                 '0 7 * * *',  $$REINDEX INDEX CONCURRENTLY nuq.queue_crawl_finished_pkey;$$);
SELECT cron.schedule('nuq_reindex_queue_crawl_finished_active_locked_at',     '20 7 * * *', $$REINDEX INDEX CONCURRENTLY nuq.queue_crawl_finished_active_locked_at_idx;$$);
SELECT cron.schedule('nuq_reindex_queue_crawl_finished_queued_optimal_2',     '40 7 * * *', $$REINDEX INDEX CONCURRENTLY nuq.nuq_queue_crawl_finished_queued_optimal_2_idx;$$);
SELECT cron.schedule('nuq_reindex_queue_crawl_finished_failed_created_at',    '0 8 * * *',  $$REINDEX INDEX CONCURRENTLY nuq.nuq_queue_crawl_finished_failed_created_at_idx;$$);
SELECT cron.schedule('nuq_reindex_queue_crawl_finished_completed_created_at', '20 8 * * *', $$REINDEX INDEX CONCURRENTLY nuq.nuq_queue_crawl_finished_completed_created_at_idx;$$);

SELECT cron.schedule('nuq_reindex_queue_crawl_finished_completed_standalone', '0 9 * * *',   $$REINDEX INDEX CONCURRENTLY nuq.nuq_queue_crawl_finished_completed_standalone_created_at_idx;$$);
SELECT cron.schedule('nuq_reindex_queue_crawl_finished_failed_standalone',    '20 9 * * *',  $$REINDEX INDEX CONCURRENTLY nuq.nuq_queue_crawl_finished_failed_standalone_created_at_idx;$$);
SELECT cron.schedule('nuq_reindex_queue_crawl_finished_group_id',             '0 10 * * *',  $$REINDEX INDEX CONCURRENTLY nuq.nuq_queue_crawl_finished_group_id_idx;$$);
SELECT cron.schedule('nuq_reindex_group_crawl_completed_expires_at',          '20 10 * * *', $$REINDEX INDEX CONCURRENTLY nuq.nuq_group_crawl_completed_expires_at_idx;$$);

CREATE TABLE IF NOT EXISTS nuq.group_crawl (
  id uuid NOT NULL,
  status nuq.group_status NOT NULL DEFAULT 'active'::nuq.group_status,
  created_at timestamptz NOT NULL DEFAULT now(),
  owner_id uuid NOT NULL,
  ttl int8 NOT NULL DEFAULT 86400000,
  expires_at timestamptz,
  CONSTRAINT group_crawl_pkey PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS idx_group_crawl_status ON nuq.group_crawl (status) WHERE status = 'active'::nuq.group_status;
CREATE INDEX IF NOT EXISTS nuq_group_crawl_completed_expires_at_idx ON nuq.group_crawl (expires_at) WHERE status = 'completed'::nuq.group_status;
CREATE INDEX IF NOT EXISTS idx_queue_scrape_backlog_group_id ON nuq.queue_scrape_backlog (group_id);

SELECT cron.schedule('nuq_group_crawl_finished', '15 seconds', $$
  WITH finished_groups AS (
    UPDATE nuq.group_crawl
    SET status = 'completed'::nuq.group_status,
        expires_at = now() + MAKE_INTERVAL(secs => nuq.group_crawl.ttl / 1000)
    WHERE status = 'active'::nuq.group_status
      AND NOT EXISTS (
        SELECT 1 FROM nuq.queue_scrape
        WHERE nuq.queue_scrape.status IN ('active', 'queued')
          AND nuq.queue_scrape.group_id = nuq.group_crawl.id
      )
      AND NOT EXISTS (
        SELECT 1 FROM nuq.queue_scrape_backlog
        WHERE nuq.queue_scrape_backlog.group_id = nuq.group_crawl.id
      )
    RETURNING id, owner_id
  )
  INSERT INTO nuq.queue_crawl_finished (data, owner_id, group_id)
  SELECT '{}'::jsonb, finished_groups.owner_id, finished_groups.id
  FROM finished_groups;
$$);

SELECT cron.schedule('nuq_group_crawl_clean', '* * * * *', $$
  SET statement_timeout = '90s';
  WITH victims AS (
    SELECT id FROM nuq.group_crawl
    WHERE status = 'completed'::nuq.group_status
      AND expires_at < now()
    ORDER BY expires_at
    LIMIT 500
    FOR UPDATE SKIP LOCKED
  ), cleaned_groups AS (
    DELETE FROM nuq.group_crawl
    WHERE id IN (SELECT id FROM victims)
    RETURNING id
  ), cleaned_jobs_queue_scrape AS (
    DELETE FROM nuq.queue_scrape
    WHERE nuq.queue_scrape.group_id IN (SELECT id FROM cleaned_groups)
  ), cleaned_jobs_queue_scrape_backlog AS (
    DELETE FROM nuq.queue_scrape_backlog
    WHERE nuq.queue_scrape_backlog.group_id IN (SELECT id FROM cleaned_groups)
  ), cleaned_jobs_crawl_finished AS (
    DELETE FROM nuq.queue_crawl_finished
    WHERE nuq.queue_crawl_finished.group_id IN (SELECT id FROM cleaned_groups)
  )
  SELECT 1;
$$);

SELECT cron.schedule('cron_job_run_details_prune', '0 * * * *', $$
  DELETE FROM cron.job_run_details WHERE start_time < now() - interval '24 hours';
$$);

SELECT pg_advisory_unlock(74920193);
