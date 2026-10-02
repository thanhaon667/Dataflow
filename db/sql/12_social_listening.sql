-- =============================================================================
-- Social listening (delivery brand): landing -> cleaning -> analysis tables
--
-- Additive: everything lives in its own schema `sl`, nothing existing is touched.
-- Input is a keyword export (CSV/Excel from a listening tool) loaded as-is into
-- sl.raw_mention; sl.run_cleaning(batch_id) turns it into sl.mention.
--
-- RUN WITH:
--   psql -U erp_app -h 127.0.0.1 -d erp_support -f db/sql/12_social_listening.sql
-- Re-runnable (IF NOT EXISTS / OR REPLACE). Dictionaries are seeded with
-- ON CONFLICT DO NOTHING so edits you make to them survive a re-run.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS sl;

-- ---------------------------------------------------------------------------
-- 1. Reference tables
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sl.brand (
    id      SERIAL PRIMARY KEY,
    name    VARCHAR(80) NOT NULL UNIQUE,
    is_own  BOOLEAN NOT NULL DEFAULT FALSE          -- the brand being studied
);

-- Search keywords. keyword_group says why it is tracked.
CREATE TABLE IF NOT EXISTS sl.keyword (
    id             SERIAL PRIMARY KEY,
    keyword        VARCHAR(200) NOT NULL UNIQUE,    -- as typed in the listening tool
    keyword_group  VARCHAR(20) NOT NULL
                   CHECK (keyword_group IN ('brand','competitor','issue','generic')),
    brand_id       INTEGER REFERENCES sl.brand(id)
);

-- Topic dictionary: a mention gets every topic whose regex matches its
-- accent-free lowercase text. Edit/add rows instead of changing code.
CREATE TABLE IF NOT EXISTS sl.topic (
    id        SERIAL PRIMARY KEY,
    code      VARCHAR(40) NOT NULL UNIQUE,
    label     VARCHAR(80) NOT NULL,
    theme     VARCHAR(30) NOT NULL,                 -- service | price | experience | promo
    pattern   TEXT NOT NULL                         -- POSIX regex on content_norm
);

-- Sentiment lexicon on accent-free lowercase terms: +1 positive, -1 negative.
CREATE TABLE IF NOT EXISTS sl.sentiment_term (
    term      VARCHAR(80) PRIMARY KEY,
    polarity  SMALLINT NOT NULL CHECK (polarity IN (-1, 1))
);

-- Posts matching any of these are marked spam (ads, giveaways, job posts, ...).
CREATE TABLE IF NOT EXISTS sl.spam_pattern (
    id       SERIAL PRIMARY KEY,
    pattern  TEXT NOT NULL UNIQUE,
    reason   VARCHAR(60) NOT NULL
);

-- Real-world events to mark on the dashboard charts (campaigns, outages, viral posts).
CREATE TABLE IF NOT EXISTS sl.event (
    id          SERIAL PRIMARY KEY,
    event_date  DATE NOT NULL,
    label       VARCHAR(120) NOT NULL
);

-- ---------------------------------------------------------------------------
-- 2. Landing: one row per exported row, text as delivered, nothing rejected
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sl.import_batch (
    id          SERIAL PRIMARY KEY,
    source_file VARCHAR(300),
    loaded_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    cleaned_at  TIMESTAMPTZ,
    rows_in     INTEGER, rows_kept INTEGER, rows_dup INTEGER, rows_spam INTEGER, rows_bad INTEGER
);

CREATE TABLE IF NOT EXISTS sl.raw_mention (
    id               BIGSERIAL PRIMARY KEY,
    batch_id         INTEGER NOT NULL REFERENCES sl.import_batch(id),
    platform         TEXT,          -- facebook, tiktok, youtube, news, forum...
    url              TEXT,
    author           TEXT,
    content          TEXT,
    posted_at        TEXT,          -- kept as text: formats differ by tool, parsed in cleaning
    likes            TEXT, comments TEXT, shares TEXT, views TEXT,
    matched_keyword  TEXT,          -- the keyword that pulled this row in
    extra            JSONB,         -- any other column of the export
    processed_at     TIMESTAMPTZ    -- set by sl.run_cleaning; NULL = still to clean
);
ALTER TABLE sl.raw_mention ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_sl_raw_batch ON sl.raw_mention(batch_id);

-- ---------------------------------------------------------------------------
-- 3. Clean mentions (analysis grain: one row per distinct post)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sl.mention (
    id               BIGSERIAL PRIMARY KEY,
    first_raw_id     BIGINT NOT NULL REFERENCES sl.raw_mention(id),
    content_hash     CHAR(32) NOT NULL UNIQUE,         -- md5(platform|norm text|author)
    platform         VARCHAR(30) NOT NULL,
    url              TEXT,
    author           VARCHAR(200),
    content          TEXT NOT NULL,                    -- cleaned, accents kept (for reading)
    content_norm     TEXT NOT NULL,                    -- lowercase, accent-free (for matching)
    posted_at        TIMESTAMPTZ NOT NULL,
    posted_date      DATE NOT NULL,
    likes            INTEGER NOT NULL DEFAULT 0,
    comments         INTEGER NOT NULL DEFAULT 0,
    shares           INTEGER NOT NULL DEFAULT 0,
    views            BIGINT  NOT NULL DEFAULT 0,
    engagement       INTEGER GENERATED ALWAYS AS (likes + comments + shares) STORED,
    brand_id         INTEGER REFERENCES sl.brand(id),  -- brand named in the text
    keyword_group    VARCHAR(20),                      -- group of the keyword that found it
    is_spam          BOOLEAN NOT NULL DEFAULT FALSE,
    spam_reason      VARCHAR(60),
    sentiment_score  SMALLINT NOT NULL DEFAULT 0,      -- positive terms - negative terms
    sentiment        VARCHAR(8) NOT NULL DEFAULT 'neutral'
                     CHECK (sentiment IN ('positive','neutral','negative')),
    keywords         TEXT[] NOT NULL DEFAULT '{}'      -- every keyword that matched it
);
CREATE INDEX IF NOT EXISTS idx_sl_mention_date  ON sl.mention(posted_date);
CREATE INDEX IF NOT EXISTS idx_sl_mention_brand ON sl.mention(brand_id, posted_date);
CREATE INDEX IF NOT EXISTS idx_sl_mention_plat  ON sl.mention(platform, posted_date);

CREATE TABLE IF NOT EXISTS sl.mention_topic (
    mention_id BIGINT  NOT NULL REFERENCES sl.mention(id) ON DELETE CASCADE,
    topic_id   INTEGER NOT NULL REFERENCES sl.topic(id),
    PRIMARY KEY (mention_id, topic_id)
);
CREATE INDEX IF NOT EXISTS idx_sl_mt_topic ON sl.mention_topic(topic_id);

-- Rows that could not be used, with the reason (kept so nothing vanishes silently).
CREATE TABLE IF NOT EXISTS sl.reject (
    raw_id BIGINT PRIMARY KEY REFERENCES sl.raw_mention(id),
    reason VARCHAR(40) NOT NULL
);

-- ---------------------------------------------------------------------------
-- 4. Cleaning helpers
-- ---------------------------------------------------------------------------
-- Accent-free, lowercase. Vietnamese diacritics only (no extension needed).
CREATE OR REPLACE FUNCTION sl.fold(t TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $$
    SELECT translate(lower(t),
        'àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ',
        'aaaaaaaaaaaaaaaaaeeeeeeeeeeeiiiiiooooooooooooooooouuuuuuuuuuuyyyyyd')
$$;

-- Strip URLs, @mentions, zero-width chars, repeated punctuation/letters, extra spaces.
CREATE OR REPLACE FUNCTION sl.clean_text(t TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $$
    SELECT btrim(regexp_replace(
           regexp_replace(
           regexp_replace(
           regexp_replace(
           regexp_replace(coalesce(t,''),
              'https?://\S+|www\.\S+', ' ', 'gi'),          -- links
              '[​‌‍﻿]', '', 'g'),       -- invisible chars
              '@\w+', ' ', 'g'),                            -- @mentions
              '(.)\1{3,}', '\1\1', 'g'),                    -- "quaaaa" -> "quaa", "!!!!!" -> "!!"
              '\s+', ' ', 'g'))
$$;

-- Number from export text: "1.2K", "3,4 tr", "1,234", "" -> integer. Garbage -> 0.
CREATE OR REPLACE FUNCTION sl.to_count(t TEXT) RETURNS BIGINT
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE s TEXT := lower(btrim(coalesce(t,''))); mult NUMERIC := 1; n NUMERIC;
BEGIN
    IF s = '' THEN RETURN 0; END IF;
    IF s ~ '(k|n|nghin|ngan)$' THEN mult := 1000;
    ELSIF s ~ '(m|tr|trieu)$'  THEN mult := 1000000; END IF;
    s := regexp_replace(s, '[a-z\s]', '', 'g');
    IF s ~ '^\d{1,3}([.,]\d{3})+$' THEN s := regexp_replace(s, '[.,]', '', 'g'); -- 1,234 / 1.234.567
    ELSE s := replace(s, ',', '.'); END IF;
    BEGIN n := s::NUMERIC; EXCEPTION WHEN others THEN RETURN 0; END;
    RETURN greatest(round(n * mult), 0)::BIGINT;
END $$;

-- Date from the usual export shapes: ISO, dd/mm/yyyy [hh:mm], dd-mm-yyyy. A value
-- without a zone is read as Vietnam time (UTC+7).
-- Returns NULL when it cannot be read or lies in the future.
CREATE OR REPLACE FUNCTION sl.to_ts(t TEXT) RETURNS TIMESTAMPTZ
LANGUAGE plpgsql STABLE AS $$
DECLARE s TEXT := btrim(coalesce(t,'')); r TIMESTAMPTZ;
BEGIN
    IF s = '' THEN RETURN NULL; END IF;
    BEGIN
        IF s ~ '^\d{1,2}[/-]\d{1,2}[/-]\d{4}' THEN          -- day first, no zone: Vietnam time
            r := regexp_replace(s, '^(\d{1,2})[/-](\d{1,2})[/-](\d{4})(.*)$', '\3-\2-\1\4')::TIMESTAMP
                 AT TIME ZONE 'Asia/Ho_Chi_Minh';
        ELSIF s ~ '\d{2}:\d{2}.*([zZ]|[+-]\d{2}(:?\d{2})?)$' THEN  -- carries its own zone
            r := s::TIMESTAMPTZ;
        ELSE                                                     -- no zone: Vietnam time
            r := s::TIMESTAMP AT TIME ZONE 'Asia/Ho_Chi_Minh';
        END IF;
    EXCEPTION WHEN others THEN RETURN NULL; END;
    IF r > now() + interval '1 day' THEN RETURN NULL; END IF;
    RETURN r;
END $$;

-- ---------------------------------------------------------------------------
-- 5. The cleaning run. Only rows with processed_at IS NULL are read, so running it twice changes nothing.
--    Steps: parse -> reject unusable -> drop exact duplicates (same text+author+platform,
--    counted across batches too) -> spam flag -> brand/topic/sentiment tagging.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION sl.run_cleaning(p_batch INTEGER) RETURNS TABLE(step TEXT, n BIGINT)
LANGUAGE plpgsql AS $$
DECLARE v_in BIGINT; v_kept BIGINT; v_dup BIGINT; v_spam BIGINT; v_bad BIGINT;
BEGIN
    SELECT count(*) INTO v_in FROM sl.raw_mention WHERE batch_id = p_batch AND processed_at IS NULL;

    -- parsed view of this batch's not-yet-processed rows
    CREATE TEMP TABLE _p ON COMMIT DROP AS
    SELECT r.id AS raw_id,
           lower(btrim(coalesce(r.platform,'')))                  AS platform,
           nullif(btrim(r.url),'')                                 AS url,
           left(nullif(btrim(r.author),''),200)                    AS author,
           sl.clean_text(r.content)                                AS content,
           sl.to_ts(r.posted_at)                                   AS posted_at,
           sl.to_count(r.likes) AS likes, sl.to_count(r.comments) AS comments,
           sl.to_count(r.shares) AS shares, sl.to_count(r.views) AS views,
           nullif(btrim(r.matched_keyword),'')                     AS kw
    FROM sl.raw_mention r
    WHERE r.batch_id = p_batch AND r.processed_at IS NULL;

    -- unusable rows
    INSERT INTO sl.reject(raw_id, reason)
    SELECT raw_id, CASE WHEN length(content) < 5 THEN 'empty_or_too_short'
                        WHEN posted_at IS NULL    THEN 'bad_date'
                        ELSE 'no_platform' END
    FROM _p WHERE length(content) < 5 OR posted_at IS NULL OR platform = ''
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS v_bad = ROW_COUNT;

    -- one row per distinct post; the keywords that found it are merged
    CREATE TEMP TABLE _d ON COMMIT DROP AS
    SELECT DISTINCT ON (h) * FROM (
        SELECT p.*, md5(p.platform||'|'||sl.fold(p.content)||'|'||coalesce(lower(p.author),'')) AS h
        FROM _p p WHERE length(p.content) >= 5 AND p.posted_at IS NOT NULL AND p.platform <> ''
    ) q ORDER BY h, (likes+comments+shares) DESC, raw_id;

    CREATE TEMP TABLE _kw ON COMMIT DROP AS
    SELECT md5(p.platform||'|'||sl.fold(p.content)||'|'||coalesce(lower(p.author),'')) AS h,
           array_agg(DISTINCT p.kw) FILTER (WHERE p.kw IS NOT NULL) AS kws
    FROM _p p GROUP BY 1;

    INSERT INTO sl.mention(first_raw_id, content_hash, platform, url, author, content, content_norm,
                           posted_at, posted_date, likes, comments, shares, views, keywords, keyword_group)
    SELECT d.raw_id, d.h, d.platform, d.url, d.author, d.content, sl.fold(d.content),
           d.posted_at, (d.posted_at AT TIME ZONE 'Asia/Ho_Chi_Minh')::DATE,
           least(d.likes, 2147483647), least(d.comments, 2147483647), least(d.shares, 2147483647), d.views,
           coalesce(k.kws, '{}'),
           (SELECT kw.keyword_group FROM sl.keyword kw WHERE kw.keyword = ANY (coalesce(k.kws,'{}'))
            ORDER BY CASE kw.keyword_group WHEN 'brand' THEN 1 WHEN 'competitor' THEN 2 WHEN 'issue' THEN 3 ELSE 4 END
            LIMIT 1)
    FROM _d d JOIN _kw k USING (h)
    ON CONFLICT (content_hash) DO UPDATE
        SET keywords = (SELECT array_agg(DISTINCT x) FROM unnest(sl.mention.keywords || EXCLUDED.keywords) x),
            likes = greatest(sl.mention.likes, EXCLUDED.likes),
            comments = greatest(sl.mention.comments, EXCLUDED.comments),
            shares = greatest(sl.mention.shares, EXCLUDED.shares),
            views = greatest(sl.mention.views, EXCLUDED.views);
    SELECT count(*) INTO v_kept FROM _d;
    SELECT (SELECT count(*) FROM _p WHERE length(content) >= 5 AND posted_at IS NOT NULL AND platform <> '') - v_kept
      INTO v_dup;

    -- everything below only touches mentions that have not been tagged yet
    UPDATE sl.mention m SET is_spam = TRUE, spam_reason = s.reason
    FROM (SELECT DISTINCT ON (m2.id) m2.id, sp.reason
          FROM sl.mention m2 JOIN sl.spam_pattern sp ON m2.content_norm ~ sp.pattern
          WHERE m2.first_raw_id IN (SELECT raw_id FROM _d)) s
    WHERE m.id = s.id;
    GET DIAGNOSTICS v_spam = ROW_COUNT;

    -- brand named in the text (own brand wins a tie)
    UPDATE sl.mention m SET brand_id = b.id
    FROM (SELECT DISTINCT ON (m2.id) m2.id, br.id
          FROM sl.mention m2 JOIN sl.brand br ON m2.content_norm LIKE '%'||sl.fold(br.name)||'%'
          WHERE m2.first_raw_id IN (SELECT raw_id FROM _d)
          ORDER BY m2.id, br.is_own DESC, br.id) b(mid, id)
    WHERE m.id = b.mid;

    INSERT INTO sl.mention_topic(mention_id, topic_id)
    SELECT m.id, t.id FROM sl.mention m JOIN sl.topic t ON m.content_norm ~ t.pattern
    WHERE m.first_raw_id IN (SELECT raw_id FROM _d)
    ON CONFLICT DO NOTHING;

    -- sentiment: whole-word lexicon hits; score>=1 positive, <=-1 negative.
    -- A hit right after "khong" / "chua" / "dung" ("khong tre", "chua thay hang tot") flips polarity.
    UPDATE sl.mention m SET sentiment_score = s.score,
        sentiment = CASE WHEN s.score >= 1 THEN 'positive' WHEN s.score <= -1 THEN 'negative' ELSE 'neutral' END
    FROM (SELECT m2.id, coalesce(sum(st.polarity * CASE WHEN m2.content_norm ~ ('(khong|chua|dung) (bi |co |con )?'||st.term||'([^a-z0-9]|$)')
                                                      THEN -1 ELSE 1 END),0)::SMALLINT AS score
          FROM sl.mention m2
          LEFT JOIN sl.sentiment_term st ON m2.content_norm ~ ('(^|[^a-z0-9])'||st.term||'([^a-z0-9]|$)')
          WHERE m2.first_raw_id IN (SELECT raw_id FROM _d)
          GROUP BY m2.id) s
    WHERE m.id = s.id;

    UPDATE sl.raw_mention SET processed_at = now() WHERE batch_id = p_batch AND processed_at IS NULL;
    -- counts add up, so rows appended to a batch later and cleaned again stay correct
    UPDATE sl.import_batch SET cleaned_at = now(),
           rows_in = coalesce(rows_in,0) + v_in,     rows_kept = coalesce(rows_kept,0) + v_kept,
           rows_dup = coalesce(rows_dup,0) + v_dup,  rows_spam = coalesce(rows_spam,0) + v_spam,
           rows_bad = coalesce(rows_bad,0) + v_bad WHERE id = p_batch;

    RETURN QUERY VALUES ('rows in', v_in), ('kept (distinct posts)', v_kept), ('duplicates merged', v_dup),
                        ('flagged spam', v_spam), ('rejected', v_bad);
END $$;

-- ---------------------------------------------------------------------------
-- 6. Analysis views (spam excluded everywhere)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW sl.v_mention AS
SELECT m.*, b.name AS brand, b.is_own
FROM sl.mention m LEFT JOIN sl.brand b ON b.id = m.brand_id
WHERE NOT m.is_spam;

CREATE OR REPLACE VIEW sl.v_daily AS
SELECT posted_date, coalesce(brand,'(none)') AS brand, platform,
       count(*) AS mentions, sum(engagement) AS engagement, sum(views) AS views,
       count(*) FILTER (WHERE sentiment='positive') AS positive,
       count(*) FILTER (WHERE sentiment='negative') AS negative,
       round(100.0*(count(*) FILTER (WHERE sentiment='positive') - count(*) FILTER (WHERE sentiment='negative'))
             / count(*), 1) AS net_sentiment_pct
FROM sl.v_mention GROUP BY 1,2,3;

CREATE OR REPLACE VIEW sl.v_topic AS
SELECT t.theme, t.label AS topic, coalesce(m.brand,'(none)') AS brand,
       count(*) AS mentions, sum(m.engagement) AS engagement,
       count(*) FILTER (WHERE m.sentiment='negative') AS negative,
       round(100.0*count(*) FILTER (WHERE m.sentiment='negative')/count(*),1) AS negative_pct
FROM sl.v_mention m
JOIN sl.mention_topic mt ON mt.mention_id = m.id
JOIN sl.topic t ON t.id = mt.topic_id
GROUP BY 1,2,3;

CREATE OR REPLACE VIEW sl.v_share_of_voice AS
SELECT month, brand, mentions,
       round(100.0*mentions/sum(mentions) OVER (PARTITION BY month), 1) AS share_pct
FROM (SELECT date_trunc('month', posted_date)::DATE AS month, brand, count(*) AS mentions
      FROM sl.v_mention WHERE brand IS NOT NULL GROUP BY 1,2) x;

CREATE OR REPLACE VIEW sl.v_top_posts AS
SELECT id, posted_date, platform, brand, sentiment, engagement, views, left(content,200) AS snippet, url
FROM sl.v_mention ORDER BY engagement DESC;

-- Rows lost at each step, per batch: the first thing to check when numbers look low.
CREATE OR REPLACE VIEW sl.v_batch_quality AS
SELECT id AS batch_id, source_file, loaded_at, rows_in, rows_kept, rows_dup, rows_spam, rows_bad,
       round(100.0*rows_kept/nullif(rows_in,0),1) AS kept_pct
FROM sl.import_batch;

-- ---------------------------------------------------------------------------
-- 7. Seed dictionaries (edit freely; re-running will not overwrite your edits)
--    Patterns are written accent-free and lowercase.
-- ---------------------------------------------------------------------------
INSERT INTO sl.topic(code, label, theme, pattern) VALUES
 ('late',      'Late delivery',            'service',    'giao (cham|tre|lau)|tre (hen|gio)|qua (han|gio)|cho (mai|hoai)|\mlate\M|delay'),
 ('lost',      'Lost parcel',       'service',    'that lac|mat (hang|don)|khong (nhan|thay) (duoc )?hang|lost'),
 ('damaged',   'Damaged goods',   'service',    'hu (hong|hai)|mop|meo|vo nat|bep dum|\mvo\M|damaged|rach'),
 ('rude',      'Courier attitude',     'service',    'shipper.*(cau gat|thai do|vo le|chui|quat)|(cau gat|vo le|thai do te)|rude'),
 ('tracking',  'Order tracking',   'experience', 'theo doi|tracking|ma van don|khong cap nhat|app (loi|lag)|trang thai don'),
 ('fee',       'Fees and pricing',           'price',      '\mphi\M|gia cuoc|\mdat\M|\mre\M|tien ship|phu thu|phi ship|cuoc phi'),
 ('cod',       'COD and refunds',     'price',      '\mcod\M|hoan tien|thu ho|doi soat|den bu|boi thuong'),
 ('support',   'Customer support',                'service',    'cskh|tong dai|hotline|ho tro|khieu nai|phan hoi|bo mac'),
 ('promo',     'Promotions',          'promo',      'khuyen mai|ma giam|voucher|freeship|mien phi ship|uu dai|sale'),
 ('fast',      'Fast delivery',          'service',    'giao nhanh|toc do|hoa toc|trong ngay|nhan hang som|dung hen'),
 ('return',    'Returns',      'service',    'hoan hang|tra hang|doi tra|giao lai|tu choi nhan')
ON CONFLICT (code) DO NOTHING;

INSERT INTO sl.sentiment_term(term, polarity) VALUES
 ('tot',1),('nhanh',1),('nhiet tinh',1),('chuyen nghiep',1),('hai long',1),('tuyet voi',1),('uy tin',1),
 ('de thuong',1),('chu dao',1),('on dinh',1),('dung hen',1),('cam on',1),('recommend',1),('xuat sac',1),
 ('tre',-1),('cham',-1),('te',-1),('toi te',-1),('do',-1),('lua dao',-1),('that vong',-1),('buc xuc',-1),
 ('buc minh',-1),('thai do',-1),('cau gat',-1),('vo le',-1),('mat hang',-1),('that lac',-1),('hu hong',-1),
 ('khong bao gio',-1),('bo mac',-1),('phien',-1),('te hai',-1),('dang tien',-1),('chan',-1),('khieu nai',-1)
ON CONFLICT (term) DO NOTHING;

INSERT INTO sl.spam_pattern(pattern, reason) VALUES
 ('tuyen (dung|shipper|tai xe|ctv)|can tuyen|tuyen gap',        'job_post'),
 ('inbox|ib ngay|lien he zalo|zalo[: ]*0\d{8,}|\m0\d{9}\M',      'sales_contact'),
 ('giveaway|minigame|mini game|trung thuong|quay so',           'giveaway'),
 ('vay (tien|nhanh)|lai suat thap|ho tro (no|vay)',             'loan_ad'),
 ('(t\.me|bit\.ly)',                                              'link_spam')
ON CONFLICT (pattern) DO NOTHING;
