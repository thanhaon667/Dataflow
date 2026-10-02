"""Generate a large, deliberately messy FAKE social-listening export for a delivery brand
and load it into sl.raw_mention (then run sl.run_cleaning). Run db/sql/12_social_listening.sql first.

  python db/sample/sl_generate_sample.py --days 180 --seed 7

Everything is invented. Brand names are placeholders; real brands go in sl.brand / sl.keyword.
Built-in story (so a dashboard has something to find):
  * double-day sales (6.6, 7.7, 8.8, 9.9) lift volume and lateness for everyone
  * mid-August FastShip sorting-hub failure: late + lost spike, negative sentiment
  * late-July viral TikTok clip about a FastShip courier: rude-courier spike
  * 1-15 September FastShip free-shipping campaign: promo mentions, better sentiment
"""
import argparse, math, os, random, sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import psycopg2, psycopg2.extras

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from erp import config  # noqa: E402

BRANDS = {  # name: (is_own, daily base volume, pos bias, neg bias)
    "FastShip":     (True, 58, 0.00, 0.00),
    "GiaoNhanh247": (False, 42, -0.03, 0.05),
    "ViettelGo":    (False, 36, 0.04, -0.03),
    "NhanhGon":     (False, 24, 0.02, 0.00),
}
KEYWORDS = [("fastship", "brand", "FastShip"), ("fast ship giao hang", "brand", "FastShip"),
            ("giaonhanh247", "competitor", "GiaoNhanh247"), ("viettelgo", "competitor", "ViettelGo"),
            ("nhanhgon", "competitor", "NhanhGon"),
            ("giao hang cham", "issue", None), ("that lac hang", "issue", None),
            ("shipper thai do", "issue", None), ("giao hang nhanh", "generic", None)]
PLATFORMS = [("facebook", 45), ("tiktok", 24), ("ecommerce", 10), ("forum", 8), ("youtube", 7), ("news", 6)]
# topic -> (weight, positive templates, negative templates, neutral templates)
T = {
 "late":    (16, ["{b} giao đúng hẹn, không trễ phút nào", "đơn hôm qua {b} giao nhanh hơn dự kiến", "giao trễ {n} ngày nhưng {b} đền bù nhanh, cảm ơn", "giao chậm một chút nhưng shipper {b} xin lỗi, tốt"],
                  ["{b} giao chậm quá, chờ {n} ngày chưa thấy hàng", "đơn trễ hẹn {n} ngày, {b} làm ăn tệ", "{b} giao trễ, bực mình thật sự"],
                  ["đơn của mình đang đi {b}, dự kiến {n} ngày nữa", "ai biết {b} giao chậm hay trễ hẹn thường mấy ngày không"]),
 "lost":    (6,  ["{b} tìm lại được đơn thất lạc, cảm ơn nhiều"],
                  ["{b} làm mất hàng của mình, tổng đài bỏ mặc", "thất lạc đơn {b}, thất vọng quá"],
                  ["có ai bị {b} mất hàng chưa nhỉ"]),
 "damaged": (7,  ["{b} đóng gói cẩn thận, hàng nguyên vẹn, chuyên nghiệp"],
                  ["hàng bị móp méo khi nhận từ {b}", "{b} làm vỡ hàng, hư hỏng hết, đền bù sao đây"],
                  ["nhận hàng {b} bị móp hộp, chưa mở"]),
 "rude":    (6,  ["shipper {b} rất nhiệt tình, lịch sự, hài lòng", "shipper {b} thái độ tốt hơn hôm trước, cảm ơn", "shipper {b} thái độ rất chuyên nghiệp"],
                  ["shipper {b} thái độ cáu gắt, vô lễ", "bực mình với shipper {b} quát khách"],
                  ["shipper {b} gọi lúc 10h tối", "hỏi về thái độ shipper {b} khu vực quận {n}"]),
 "tracking":(8,  ["app {b} theo dõi đơn rõ ràng, ổn định"],
                  ["không theo dõi được mã vận đơn {b}, app lỗi hoài", "app {b} không cập nhật trạng thái, phiền quá"],
                  ["mã vận đơn {b} dạng nào vậy mọi người"]),
 "fee":     (10, ["phí ship {b} rẻ, tốt hơn nhiều chỗ khác"],
                  ["phí ship {b} đắt quá, không đáng tiền", "{b} phụ thu vô lý, buc xuc"],
                  ["bảng giá cước {b} mới tăng {n}k"]),
 "cod":     (6,  ["{b} hoàn tiền COD nhanh, uy tín"],
                  ["đòi hoàn tiền COD của {b} mãi chưa được, lừa đảo", "{b} đối soát COD sai, khiếu nại bị bỏ mặc"],
                  ["thu hộ COD bên {b} mất mấy ngày vậy"]),
 "support": (8,  ["CSKH {b} hỗ trợ tận tình, cảm ơn", "tổng đài {b} xử lý nhanh, hài lòng"],
                  ["gọi hotline {b} không ai nhấc máy, tệ", "khiếu nại {b} bị bỏ mặc, thất vọng"],
                  ["hotline {b} số mấy vậy mọi người"]),
 "promo":   (7,  ["mã giảm giá {b} dùng ngon, ưu đãi tốt", "freeship {b} tuyệt vời"],
                  ["mã voucher {b} bị lỗi, thất vọng"],
                  ["có mã giảm giá {b} tuần này không"]),
 "fast":    (12, ["{b} giao nhanh, đúng hẹn, tuyệt vời", "giao hỏa tốc {b} trong ngày, chuyên nghiệp"],
                  ["giao nhanh gì mà chậm, {b} thất vọng"],
                  ["so sánh tốc độ giao hàng {b} với mấy bên"]),
 "other":   (14, ["{b} dịch vụ ổn, sẽ ủng hộ tiếp", "dùng {b} lâu rồi, hài lòng"],
                  ["{b} chán thật", "dịch vụ {b} tệ"],
                  ["review {b} sau 1 tháng dùng", "có nên dùng {b} cho shop nhỏ không", "{b} vừa mở bưu cục mới ở quận {n}"]),
}
SPAM = ["Tuyển shipper {b} gấp, lương cao, inbox zalo 09{r}", "MINIGAME trúng thưởng freeship cùng {b}, quay số mỗi ngày",
        "vay tiền nhanh lãi suất thấp, ib ngay 09{r}", "cần tuyển CTV giao hàng {b}, liên hệ zalo 09{r}"]
EMOJI = ["", "", "", " 👍", " 😡", " 😤", " 🥲", " ❤️", " 🙏"]
SLANG = {"không": "ko", "được": "dc", "rất": "r", "quá": "wa", "mình": "mk"}
DAYMULT = [0.95, 1.05, 1.05, 1.0, 1.05, 0.9, 0.85]  # Mon..Sun
HOURW = [1,1,1,1,1,2,3,5,7,8,9,10,10,8,7,7,8,9,11,12,12,10,6,3]


def strip_accents(s):
    import unicodedata
    s = s.replace("đ", "d").replace("Đ", "D")
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def day_mult(d, brand, topic_weights):
    """Volume multiplier for a brand on day d, plus per-topic and sentiment shifts."""
    m, shift, tw = 1.0, 0.0, dict(topic_weights)
    for md in [(6, 6), (7, 7), (8, 8), (9, 9)]:
        k = (d - date(d.year, *md)).days
        if 0 <= k <= 2:
            m *= 1.6 - 0.2 * k; tw["late"] *= 2.2; tw["lost"] *= 1.6; shift -= 0.06
    if brand == "FastShip":
        k = (d - date(d.year, 8, 14)).days
        if 0 <= k <= 14:
            f = math.exp(-k / 5.0); m *= 1 + 2.4 * f; tw["late"] *= 1 + 5 * f; tw["lost"] *= 1 + 7 * f; shift -= 0.30 * f
        k = (d - date(d.year, 7, 22)).days
        if 0 <= k <= 10:
            f = math.exp(-k / 3.0); m *= 1 + 1.8 * f; tw["rude"] *= 1 + 14 * f; shift -= 0.25 * f
        if date(d.year, 9, 1) <= d <= date(d.year, 9, 15):
            m *= 1.35; tw["promo"] *= 6; tw["fee"] *= 0.6; shift += 0.15
        m *= 1 + 0.0012 * (d - (date.today() - timedelta(days=180))).days  # slow organic growth
    return m, shift, tw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--keep", action="store_true", help="do not wipe existing sl data first")
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    conn = psycopg2.connect(host=config.DB_HOST, port=config.DB_PORT, dbname=config.DB_NAME,
                            user=config.DB_USER, password=config.DB_PASSWORD)
    cur = conn.cursor()
    if not a.keep:
        cur.execute("TRUNCATE sl.mention_topic, sl.mention, sl.reject, sl.raw_mention, sl.import_batch, "
                    "sl.keyword, sl.brand RESTART IDENTITY CASCADE")
    for n, (own, *_r) in BRANDS.items():
        cur.execute("INSERT INTO sl.brand(name,is_own) VALUES (%s,%s) ON CONFLICT (name) DO NOTHING", (n, own))
    for kw, g, b in KEYWORDS:
        cur.execute("INSERT INTO sl.keyword(keyword,keyword_group,brand_id) VALUES (%s,%s,(SELECT id FROM sl.brand WHERE name=%s)) "
                    "ON CONFLICT (keyword) DO NOTHING", (kw, g, b))
    if not a.keep:
        y = date.today().year
        cur.executemany("INSERT INTO sl.event(event_date,label) VALUES (%s,%s)", [
            (date(y, 6, 6), "Sale 6.6"), (date(y, 7, 7), "Sale 7.7"), (date(y, 7, 22), "TikTok clip about a FastShip courier"),
            (date(y, 8, 8), "Sale 8.8"), (date(y, 8, 14), "FastShip sorting-hub failure"),
            (date(y, 9, 1), "FastShip free-shipping campaign (1-15 Sep)"), (date(y, 9, 9), "Sale 9.9")])
    cur.execute("INSERT INTO sl.import_batch(source_file) VALUES ('fake_export_%s_days.csv') RETURNING id", (a.days,))
    batch = cur.fetchone()[0]
    authors = [f"{rnd.choice(['nguyen','tran','le','pham','hoang','vu','dang','bui'])}_{rnd.choice(['an','binh','chi','dung','ha','khoa','lan','minh','nam','phuc','quang','thu','trang'])}{rnd.randint(1,999)}" for _ in range(2500)]
    pw, pn = [w for _, w in PLATFORMS], [n for n, _ in PLATFORMS]
    topics = list(T); base_tw = {k: v[0] for k, v in T.items()}
    today = date.today(); rows = []
    for off in range(a.days, -1, -1):
        d = today - timedelta(days=off)
        for brand, (own, base, pb, nb) in BRANDS.items():
            m, shift, tw = day_mult(d, brand, base_tw)
            n = max(0, int(rnd.gauss(base * m * DAYMULT[d.weekday()], base * 0.12)))
            weights = [tw[t] for t in topics]
            for _ in range(n):
                t = rnd.choices(topics, weights)[0]
                if t == "rude" and brand != "FastShip" and rnd.random() < 0.5: t = "other"
                ps = 0.30 + pb - shift * 0.8 + (0.35 if t in ("fast", "promo") else 0) - (0.2 if t in ("late","lost","damaged","rude") else 0)
                ns = 0.22 + nb + shift * -1 * 0.9 + (0.45 if t in ("late","lost","damaged","rude","cod") else 0) - (0.1 if t in ("fast","promo") else 0)
                ps, ns = min(max(ps, 0.03), 0.85), min(max(ns, 0.03), 0.85)
                x = rnd.random() * (ps + ns + 0.30)
                pos, neg, neu = T[t][1], T[t][2], T[t][3]
                tpl = rnd.choice(pos) if x < ps else rnd.choice(neg) if x < ps + ns else rnd.choice(neu)
                txt = tpl.format(b=brand if rnd.random() < .8 else brand.lower(), n=rnd.randint(2, 9)) + rnd.choice(EMOJI)
                if rnd.random() < .12: txt += "!!!!!"
                if rnd.random() < .10: txt += " https://fb.me/" + str(rnd.randint(10**6, 10**7))
                if rnd.random() < .08: txt += " @" + rnd.choice(authors)
                if rnd.random() < .15:
                    for k, v in SLANG.items(): txt = txt.replace(k, v)
                if rnd.random() < .30: txt = strip_accents(txt)
                rows.append((d, brand, t, txt, pw, pn))
        # spam, unrelated to brand story
        for _ in range(int(rnd.gauss(14, 3))):
            rows.append((d, rnd.choice(list(BRANDS)), "spam", rnd.choice(SPAM).format(b=rnd.choice(list(BRANDS)), r=rnd.randint(10**7, 10**8 - 1)), pw, pn))
    out = []
    for d, brand, t, txt, pw, pn in rows:
        plat = rnd.choices(pn, pw)[0]
        if t == "rude" and d.month == 7 and d.day >= 22 and rnd.random() < .6: plat = "tiktok"
        hr = rnd.choices(range(24), HOURW)[0]
        ts = datetime(d.year, d.month, d.day, hr, rnd.randint(0, 59), rnd.randint(0, 59))
        f = rnd.random()
        pts = (ts.strftime("%d/%m/%Y %H:%M") if f < .25 else ts.strftime("%Y-%m-%dT%H:%M:%S+07:00") if f < .5 else
               ts.strftime("%d-%m-%Y") if f < .65 else ts.strftime("%Y-%m-%d %H:%M:%S") if f < .97 else rnd.choice(["", "n/a", "khong ro"]))
        sc = {"tiktok": 3.2, "facebook": 1.6, "youtube": 1.9, "ecommerce": 0.6, "forum": 0.9, "news": 0.7}[plat]
        boost = 1 + (6 if (t == "rude" and plat == "tiktok" and d.month == 7) else 0) + (3 if (brand == "FastShip" and d.month == 8 and 14 <= d.day <= 20 and t in ("late", "lost")) else 0)
        likes = int(rnd.lognormvariate(sc, 1.3) * boost)
        def fmt(v):
            r = rnd.random()
            return (f"{v/1000:.1f}K" if v >= 1000 and r < .4 else f"{v:,}" if v >= 1000 and r < .7 else "" if r > .985 else str(v))
        cm, sh = int(likes * rnd.uniform(.03, .25)), int(likes * rnd.uniform(.01, .12))
        views = int(likes * rnd.uniform(12, 60)) if plat in ("tiktok", "youtube") else int(likes * rnd.uniform(5, 25))
        kw = (rnd.choice(["fastship", "fast ship giao hang"]) if brand == "FastShip" else brand.lower()) if rnd.random() < .6 else \
             {"late": "giao hang cham", "lost": "that lac hang", "rude": "shipper thai do"}.get(t, "giao hang nhanh")
        if t == "spam": kw = "giao hang nhanh"
        out.append((batch, plat, f"https://{plat}.example/p/{rnd.randint(10**8, 10**9)}", rnd.choice(authors), txt, pts,
                    fmt(likes), fmt(cm), fmt(sh), fmt(views), kw))
    # the same post found again by another keyword, plus some exact repeats
    for r in rnd.sample(out, int(len(out) * .09)):
        out.append(r[:10] + (rnd.choice(["giao hang cham", "giao hang nhanh", "fastship"]),))
    rnd.shuffle(out)
    psycopg2.extras.execute_values(cur, "INSERT INTO sl.raw_mention(batch_id,platform,url,author,content,posted_at,likes,comments,shares,views,matched_keyword) VALUES %s", out, page_size=5000)
    conn.commit()
    cur.execute("SELECT * FROM sl.run_cleaning(%s)", (batch,)); print(cur.fetchall()); conn.commit()


if __name__ == "__main__":
    main()
