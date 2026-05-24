-- Run this in Supabase → SQL Editor

CREATE TABLE IF NOT EXISTS jobs (
  id               UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
  company          TEXT        NOT NULL,
  title            TEXT        NOT NULL,
  url              TEXT        NOT NULL UNIQUE,
  location         TEXT,
  is_active        BOOLEAN     DEFAULT TRUE,
  first_seen       TIMESTAMPTZ DEFAULT NOW(),
  last_seen        TIMESTAMPTZ DEFAULT NOW(),

  -- Full job description (free text)
  description      TEXT,

  -- Structured extraction (arrays for filtering)
  hard_skills      TEXT[]      DEFAULT '{}',
  soft_skills      TEXT[]      DEFAULT '{}',
  years_experience TEXT,       -- e.g. "3-5" / "5+" / "1"
  seniority        TEXT,       -- Junior / Mid / Senior / Lead / Manager
  employment_type  TEXT,       -- Full-time / Part-time / Contract

  -- Track whether we've fetched details for this job
  details_fetched  BOOLEAN     DEFAULT FALSE,
  details_fetched_at TIMESTAMPTZ
);

-- Indexes for fast filtering
CREATE INDEX IF NOT EXISTS jobs_company_idx      ON jobs (company);
CREATE INDEX IF NOT EXISTS jobs_first_seen_idx   ON jobs (first_seen DESC);
CREATE INDEX IF NOT EXISTS jobs_is_active_idx    ON jobs (is_active);
CREATE INDEX IF NOT EXISTS jobs_seniority_idx    ON jobs (seniority);
CREATE INDEX IF NOT EXISTS jobs_hard_skills_idx  ON jobs USING GIN (hard_skills);
CREATE INDEX IF NOT EXISTS jobs_soft_skills_idx  ON jobs USING GIN (soft_skills);

-- Allow anonymous read (for the website)
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Anyone can read active jobs"
  ON jobs FOR SELECT
  USING (is_active = TRUE);
