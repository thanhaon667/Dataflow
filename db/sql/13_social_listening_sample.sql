-- =============================================================================
-- Social listening: SAMPLE data only (made up) to try the pipeline end to end.
-- Brand "FastShip" stands in for the real brand; rename it in sl.brand later.
-- Loads one deliberately messy batch, then cleans it:
--   RUN: psql ... -f db/sql/12_social_listening.sql -f db/sql/13_social_listening_sample.sql
-- Do not run on a database that holds real listening data.
-- =============================================================================
INSERT INTO sl.brand(name,is_own) VALUES ('FastShip',TRUE),('GiaoNhanh247',FALSE),('ViettelGo',FALSE)
ON CONFLICT (name) DO NOTHING;

INSERT INTO sl.keyword(keyword,keyword_group,brand_id)
SELECT k.kw,k.grp,(SELECT id FROM sl.brand WHERE name=k.b) FROM (VALUES
 ('fastship','brand','FastShip'),('fast ship giao hang','brand','FastShip'),
 ('giaonhanh247','competitor','GiaoNhanh247'),('viettelgo','competitor','ViettelGo'),
 ('giao hang cham','issue',NULL),('that lac hang','issue',NULL),('shipper thai do','issue',NULL),
 ('giao hang nhanh','generic',NULL)) k(kw,grp,b)
ON CONFLICT (keyword) DO NOTHING;

WITH b AS (INSERT INTO sl.import_batch(source_file) VALUES ('sample_keyword_export.csv') RETURNING id)
INSERT INTO sl.raw_mention(batch_id,platform,url,author,content,posted_at,likes,comments,shares,views,matched_keyword)
SELECT b.id, p, 'https://example.com/p/'||g, 'user'||(g%40),
  (ARRAY[
   'FastShip giao chậm quá, chờ 5 ngày chưa thấy hàng, shipper thái độ cáu gắt!!!!!',
   'Cảm ơn FastShip giao nhanh, đúng hẹn, shipper nhiệt tình 👍 https://t.co/abc',
   'GiaoNhanh247 làm mất hàng của mình, tổng đài bỏ mặc, quá thất vọng',
   'ViettelGo phí ship đắt hơn FastShip nhiều, nhưng giao ổn định',
   'Hàng bị móp méo khi nhận từ fastship, đòi hoàn tiền COD mãi chưa được',
   'Tuyển shipper gấp, lương cao, inbox zalo 0912345678',
   'MINIGAME trúng thưởng freeship cùng FastShip, quay số mỗi ngày',
   'Không theo dõi được mã vận đơn, app FastShip lỗi hoài @fastship_cskh',
   'mã giảm giá FastShip hôm nay, freeship toàn quốc'
  ])[1+g%9],
  CASE g%6 WHEN 0 THEN to_char(now()-((g%60)||' days')::interval,'DD/MM/YYYY HH24:MI')
           WHEN 1 THEN to_char(now()-((g%60)||' days')::interval,'YYYY-MM-DD"T"HH24:MI:SS"+07:00"')
           WHEN 2 THEN to_char(now()-((g%60)||' days')::interval,'DD-MM-YYYY')
           WHEN 3 THEN 'khong ro'                                    -- unreadable date
           ELSE to_char(now()-((g%60)||' days')::interval,'YYYY-MM-DD HH24:MI:SS') END,
  CASE g%5 WHEN 0 THEN '1,234' WHEN 1 THEN '2.5K' WHEN 2 THEN '' ELSE (g%90)::text END,
  (g%12)::text, CASE WHEN g%7=0 THEN '1,2 tr' ELSE (g%20)::text END, (g*37)::text,
  (ARRAY['fastship','giaonhanh247','viettelgo','giao hang cham','that lac hang','shipper thai do','giao hang nhanh'])[1+g%7]
FROM b, generate_series(1,600) g,
     LATERAL (SELECT (ARRAY['facebook','tiktok','youtube','news','forum'])[1+g%5] AS p) q;

-- same post found again by another keyword + exact repeat
INSERT INTO sl.raw_mention(batch_id,platform,url,author,content,posted_at,likes,comments,shares,views,matched_keyword)
SELECT batch_id,platform,url,author,content,posted_at,likes,comments,shares,views,'giao hang cham'
FROM sl.raw_mention WHERE id <= 60;

SELECT * FROM sl.run_cleaning((SELECT max(id) FROM sl.import_batch));
