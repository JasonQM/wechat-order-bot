"""
订单库：SQLite 建表、写入、查询。

设计原则
--------
1. 金额一律用「分」的整数存储，绝不存浮点。展示时才转元。
2. created_at 统一 ISO 本地时间字符串，便于按天切片。
3. 开启 WAL，允许机器人进程与 Web 进程同时读写。
"""

import os
import sqlite3
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE_DIR)
DATA_DIR = os.path.join(ROOT, "data")
IMAGE_DIR = os.path.join(DATA_DIR, "images")
DB_PATH = os.path.join(DATA_DIR, "orders.db")

DEFAULT_DISCOUNT = 0.95

# 订单状态
STATUS_LABELS = {
    "processing": "识别中",
    "ready": "待发送",
    "auto_sent": "自动发送",
    "manual_pending": "待人工",
    "manual_sent": "人工放行",
    "failed": "失败",
    "ignored": "已忽略",
}

STATUS_TONE = {
    "processing": "info",
    "ready": "warn",
    "auto_sent": "ok",
    "manual_pending": "warn",
    "manual_sent": "info",
    "failed": "bad",
    "ignored": "muted",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT    NOT NULL,
    group_name      TEXT    NOT NULL,
    sender          TEXT,
    image_path      TEXT,
    image_sha256    TEXT,
    thumb_sha       TEXT,
    ocr_total_cents INTEGER,
    discount_rate   REAL    NOT NULL DEFAULT 0.95,
    final_cents     INTEGER NOT NULL,
    min_confidence  REAL,
    status          TEXT    NOT NULL DEFAULT 'manual_pending',
    at_result       TEXT,
    elapsed_ms      INTEGER,
    detail_check    TEXT,
    ocr_text        TEXT,
    model_json      TEXT,
    note            TEXT,
    replied         INTEGER NOT NULL DEFAULT 0,
    replied_at      TEXT,
    text_sent       INTEGER NOT NULL DEFAULT 0,
    image_sent      INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status  ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_group   ON orders(group_name);

-- 缩略图观察记录：一张订单图在聊天窗口里出现过的每一处「指纹」都记一行。
--
-- 为什么不能只往 orders 里存一个 thumb_sha：
-- 同一张照片可能被连发好几次，每次在聊天区里占一个位置，图片框检测出来的
-- 高度也各不相同（实测同一张照片三处位置分别高 168/181/172），
-- 于是同一张图算出三个不同的指纹。只存一列就只能记住最后一个位置，
-- 另外两处下次照样得点开。
-- 一对多之后，三个位置都能在点开之前认出来。
CREATE TABLE IF NOT EXISTS seen_thumbs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_name  TEXT    NOT NULL,
    thumb_sha   TEXT    NOT NULL,
    order_id    INTEGER,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seen_thumbs_group ON seen_thumbs(group_name, id DESC);
"""
# 注意：idx_orders_sha 不能写在上面——老库还没有 image_sha256 列时建索引会直接报错。
# 它由 init_db() 在补完列之后单独创建。

# 老库补列：CREATE TABLE IF NOT EXISTS 不会给已存在的表加字段
MIGRATIONS = [
    ("image_sha256", "ALTER TABLE orders ADD COLUMN image_sha256 TEXT"),
    ("replied", "ALTER TABLE orders ADD COLUMN replied INTEGER NOT NULL DEFAULT 0"),
    ("replied_at", "ALTER TABLE orders ADD COLUMN replied_at TEXT"),
    # 实际发出去的那张固定图片指纹。留它是为了「发的图到底是不是最新那张」
    # 这类问题以后能直接查记录，不用再靠翻日志对时间。
    ("reply_image_fp", "ALTER TABLE orders ADD COLUMN reply_image_fp TEXT"),
    # 发送是两步（发文本 + 发收款码），中间可能崩掉。
    # 只有一个状态字段的话，"文本发了图没发"和"啥都没发"都看不出来，
    # 重启后要靠翻微信记录才知道该补哪一步。
    ("text_sent", "ALTER TABLE orders ADD COLUMN text_sent INTEGER NOT NULL DEFAULT 0"),
    ("image_sent", "ALTER TABLE orders ADD COLUMN image_sent INTEGER NOT NULL DEFAULT 0"),
    # 聊天窗口里那张【缩略图】的 dHash。
    #
    # 为什么必须和 image_sha256 分开存：两者不是同一种东西。
    # image_sha256 是点开后预览窗里的大图算出来的，thumb_sha 是聊天区里
    # 那张 200px 缩略图算出来的。同一张图这两个指纹完全不同（尺寸差一个数量级），
    # 混在一列里比较必然全部对不上。
    #
    # 早先就是因为只有大图指纹，点开前拿缩略图去查永远查不到，
    # 于是每张图都必须先点开、抓图、算指纹，才发现"这单处理过了"——
    # 这就是"图片被反复打开"的根因。
    ("thumb_sha", "ALTER TABLE orders ADD COLUMN thumb_sha TEXT"),
]


def connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(IMAGE_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}
        for col, ddl in MIGRATIONS:
            if col not in existing:
                conn.execute(ddl)
        # 补列的索引要单独建（CREATE INDEX 在列不存在时会失败）
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_sha "
            "ON orders(group_name, image_sha256) WHERE image_sha256 IS NOT NULL"
        )
        # 缩略图指纹只做普通索引：它是"近似匹配"（汉明距离），不是全等，
        # 而且允许为空（旧订单没有）。加 UNIQUE 会在同一群两张图撞指纹时直接插入失败。
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_thumb "
            "ON orders(group_name, thumb_sha)"
        )
        # 历史数据补齐：凡是"已发送"状态但 replied 还是 0 的，补成 1
        conn.execute(
            "UPDATE orders SET replied=1, "
            "replied_at=COALESCE(replied_at, updated_at, created_at) "
            "WHERE status IN ('auto_sent','manual_sent') AND replied=0"
        )


# ---------------------------------------------------------------- 工具
def yuan(cents):
    """分 → 元，两位小数字符串。"""
    if cents is None:
        return "-"
    return f"{cents / 100.0:.2f}"


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def days_ago_str(days):
    return (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- KPI
def fetch_kpi(day=None):
    day = day or today_str()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
              COUNT(*)                                              AS total,
              COALESCE(SUM(final_cents), 0)                          AS amount,
              COALESCE(SUM(ocr_total_cents) - SUM(final_cents), 0)   AS cut,
              SUM(CASE WHEN status='manual_pending' THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN status='failed'        THEN 1 ELSE 0 END) AS failed,
              SUM(CASE WHEN status='auto_sent'     THEN 1 ELSE 0 END) AS auto_sent,
              SUM(CASE WHEN replied=1              THEN 1 ELSE 0 END) AS replied,
              SUM(CASE WHEN replied=0              THEN 1 ELSE 0 END) AS unreplied,
              CAST(AVG(elapsed_ms) AS INTEGER)                       AS avg_ms
            FROM orders
            WHERE substr(created_at, 1, 10) = ?
            """,
            (day,),
        ).fetchone()

        all_pending = conn.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE status='manual_pending'"
        ).fetchone()["c"]

    return {
        "total": row["total"] or 0,
        "amount": row["amount"] or 0,
        "cut": row["cut"] or 0,
        "pending": row["pending"] or 0,
        "failed": row["failed"] or 0,
        "auto_sent": row["auto_sent"] or 0,
        "replied": row["replied"] or 0,
        "unreplied": row["unreplied"] or 0,
        "avg_ms": row["avg_ms"] or 0,
        "all_pending": all_pending,
    }


# ---------------------------------------------------------------- 趋势
def fetch_daily_series(days=14):
    start = days_ago_str(days)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT substr(created_at, 1, 10) AS d,
                   COUNT(*)                  AS cnt,
                   COALESCE(SUM(final_cents), 0) AS amount
            FROM orders
            WHERE substr(created_at, 1, 10) >= ?
            GROUP BY d ORDER BY d
            """,
            (start,),
        ).fetchall()

    bucket = {r["d"]: (r["cnt"], r["amount"]) for r in rows}
    labels, counts, amounts = [], [], []
    base = datetime.now()
    for i in range(days - 1, -1, -1):
        d = (base - timedelta(days=i)).strftime("%Y-%m-%d")
        labels.append(d[5:])  # MM-DD
        c, a = bucket.get(d, (0, 0))
        counts.append(c)
        amounts.append(round(a / 100.0, 2))
    return {"labels": labels, "counts": counts, "amounts": amounts}


def fetch_group_dist(days=14):
    start = days_ago_str(days)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT group_name, COUNT(*) AS cnt, COALESCE(SUM(final_cents),0) AS amount
            FROM orders
            WHERE substr(created_at, 1, 10) >= ?
            GROUP BY group_name ORDER BY cnt DESC LIMIT 12
            """,
            (start,),
        ).fetchall()
    return [
        {"name": r["group_name"], "count": r["cnt"], "amount": round(r["amount"] / 100.0, 2)}
        for r in rows
    ]


def fetch_status_dist(days=14):
    start = days_ago_str(days)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT status, COUNT(*) AS cnt FROM orders
            WHERE substr(created_at, 1, 10) >= ?
            GROUP BY status
            """,
            (start,),
        ).fetchall()
    return [
        {"name": STATUS_LABELS.get(r["status"], r["status"]), "value": r["cnt"]}
        for r in rows
    ]


# ---------------------------------------------------------------- 列表
def fetch_orders(date_from=None, date_to=None, group=None, status=None,
                 keyword=None, page=1, page_size=20):
    where, params = [], []

    if date_from:
        where.append("substr(created_at,1,10) >= ?")
        params.append(date_from)
    if date_to:
        where.append("substr(created_at,1,10) <= ?")
        params.append(date_to)
    if group:
        where.append("group_name = ?")
        params.append(group)
    if status:
        where.append("status = ?")
        params.append(status)
    if keyword:
        where.append("(sender LIKE ? OR group_name LIKE ? OR note LIKE ?)")
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw])

    clause = ("WHERE " + " AND ".join(where)) if where else ""

    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS c FROM orders {clause}", params).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM orders {clause} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            params + [page_size, (page - 1) * page_size],
        ).fetchall()

    return rows, total


def fetch_groups():
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT group_name FROM orders ORDER BY group_name"
        ).fetchall()
    return [r["group_name"] for r in rows]


def fetch_pending(limit=200):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE status='manual_pending' "
            "ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
    return rows


def fetch_order(order_id):
    with connect() as conn:
        return conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()


def set_status(order_id, status, note=None):
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    with connect() as conn:
        conn.execute(
            "UPDATE orders SET status=?, note=COALESCE(?, note), updated_at=? WHERE id=?",
            (status, note, now, order_id),
        )


def hamming_hex(a, b):
    """两个十六进制哈希之间的汉明距离（不同位的个数）。"""
    try:
        return bin(int(str(a), 16) ^ int(str(b), 16)).count("1")
    except (ValueError, TypeError):
        return 999


# 图片指纹的两档阈值。实测数据：
#   同一张图不同抓取的抖动：4 / 7 / 7（最大 7）
#   不同照片之间的距离：80 ~ 117（最小 80）
# 所以 7 以内可以确定是同一张图；8~12 是"不敢确定"的灰区。
#
# 灰区必须转人工，不能直接判重复丢掉：同一台加油机、同一个角度，
# 两次加油只改了金额，画面结构几乎一样，感知哈希的距离会非常小。
# 这种图被当成重复丢弃，就是实实在在的漏单 —— 客户发了新订单没人回。
SHA_EXACT = 7


def find_by_sha(group_name, image_sha256, max_dist=12, limit=400, with_dist=False):
    """
    按「群 + 图片内容指纹」查是否已处理过。这是防重复报价的主键。

    为什么用汉明距离而不是全等：
    同一张图从聊天窗口里点开抓两次，抓到的画面会有细微差别
    （预览缩放、加载时机、抗锯齿），指纹就不可能一模一样。
    实测同一张图的三个版本互相距离是 4、7、7，而不同图片之间是 99 以上。
    早先按全等比较，判不出重复 → 同一张订单被当成三笔，识别三次、报价三次。

    max_dist=12 是个安全间隔：远大于同图抖动（≤7），远小于异图差异（≥99）。
    """
    if not image_sha256:
        return None

    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE group_name=? AND image_sha256 IS NOT NULL "
            "ORDER BY id DESC LIMIT ?",
            (group_name, limit),
        ).fetchall()

    best, best_d = None, max_dist + 1
    for r in rows:
        d = hamming_hex(r["image_sha256"], image_sha256)
        if d < best_d:
            best, best_d = r, d
    hit = best if best_d <= max_dist else None
    if with_dist:
        # 连距离一起返回：调用方需要区分"确定是同一张图"（≤SHA_EXACT）
        # 和"看起来像但不敢确定"（灰区），后者要转人工，不能直接丢弃。
        return hit, best_d
    return hit


# 缩略图指纹的汉明距离阈值。
#
# 这个数比大图的 12 严得多，原因是缩略图信息量小：
# 加油机照片的缩略图都是"白屏 + 一块深色数字区"，两张不同的订单拍出来
# 结构高度相似，距离天然就近。放宽到 12 会把不同订单判成同一单 —— 那是漏单，
# 比重复点开严重得多。
#
# 6 的取法：同一张缩略图两次抓取的抖动实测在 0~4 位（像素对齐差异），
# 不同订单实测 >= 18。取 6 留了余量又远离误判区。
THUMB_MAX_DIST = 6


def find_by_thumb(group_name, thumb_sha, max_dist=None, limit=800):
    """
    点开前的预判：这处缩略图是不是已经处理过？

    查 seen_thumbs（一张订单图可能有多处位置、多个指纹），联查订单状态。
    返回 {"order_id": .., "status": ..} 或 None。

    为什么要联查状态：明确已发送/待人工的才跳过；
    failed 的要允许重试，不能因为"见过"就永远放弃。
    """
    if not thumb_sha:
        return None
    md = THUMB_MAX_DIST if max_dist is None else int(max_dist)

    with connect() as conn:
        rows = conn.execute(
            "SELECT t.thumb_sha AS k, t.order_id AS oid, o.status AS st "
            "FROM seen_thumbs t LEFT JOIN orders o ON o.id = t.order_id "
            "WHERE t.group_name=? ORDER BY t.id DESC LIMIT ?",
            (group_name, limit),
        ).fetchall()

    best, best_d = None, md + 1
    for r in rows:
        d = hamming_hex(r["k"], thumb_sha)
        if d < best_d:
            best, best_d = r, d
    if best is None or best_d > md:
        return None
    return {"order_id": best["oid"], "status": best["st"]}


def add_seen_thumb(group_name, thumb_sha, order_id=None, max_dist=None):
    """
    记一处缩略图观察。已经有足够接近的记录就不重复插入。

    去重很重要：这张图每轮扫描都会被看到一次，不过滤的话表会无限膨胀，
    查一次要扫几万行，判重反而变成新的性能瓶颈。
    """
    if not thumb_sha:
        return False
    if find_by_thumb(group_name, thumb_sha, max_dist) is not None:
        return False

    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    with connect() as conn:
        conn.execute(
            "INSERT INTO seen_thumbs (group_name, thumb_sha, order_id, created_at) "
            "VALUES (?,?,?,?)",
            (group_name, thumb_sha, order_id, now),
        )
    return True


def forget_thumb(order_id):
    """
    删掉指向某订单的缩略图观察记录。

    重试一单之前必须调它：否则机器人下一轮进群时，会在【点开之前】就凭
    seen_thumbs 把这处缩略图认成"已建过单"直接跳过，重试等于没发生。
    """
    with connect() as conn:
        cur = conn.execute("DELETE FROM seen_thumbs WHERE order_id=?", (order_id,))
        return cur.rowcount


def prune_seen_thumbs(keep_days=30):
    """删掉太老的观察记录。群聊里的图会越滚越远，留 30 天足够。"""
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%dT%H:%M:%S")
    with connect() as conn:
        cur = conn.execute("DELETE FROM seen_thumbs WHERE created_at < ?", (cutoff,))
        return cur.rowcount


def insert_order(created_at, group_name, sender, image_path,
                 ocr_total_cents, discount_rate=0.95, status="manual_pending",
                 at_result=None, elapsed_ms=None, detail_check=None,
                 min_confidence=None, ocr_text=None, model_json=None, note=None,
                 image_sha256=None):
    """
    写入一笔订单。final_cents 由 ocr_total_cents × 折扣 四舍五入到分得出。

    这里用整数百分比运算（不是 float 乘法），和 src/pricing.py 保持一致：
        final = (total × pct + 50) // 100
    119.80 元 95 折 → 11381 分，不会出现 11380.9999 这种浮点尾巴。
    """
    if ocr_total_cents is None:
        final_cents = 0
    else:
        pct = int(round(float(discount_rate) * 100))
        final_cents = (int(ocr_total_cents) * pct + 50) // 100

    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO orders
              (created_at, group_name, sender, image_path, image_sha256,
               ocr_total_cents, discount_rate, final_cents, min_confidence,
               status, at_result, elapsed_ms, detail_check, ocr_text,
               model_json, note, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (created_at, group_name, sender, image_path, image_sha256,
             ocr_total_cents, discount_rate, final_cents, min_confidence,
             status, at_result, elapsed_ms, detail_check, ocr_text,
             model_json, note, now),
        )
        return cur.lastrowid


if __name__ == "__main__":
    init_db()
    print(f"数据库已初始化: {DB_PATH}")

# ---------------------------------------------------------------- 两阶段写入
def create_order(group_name, sender, image_path, image_sha256,
                 created_at=None, status="processing", note=None, thumb_sha=None):
    """
    第一阶段：先落单。

    不管后面识别成功还是失败，这一条先记下来 —— 这样"这笔订单存在过"就有据可查，
    中途崩溃也不会凭空消失。金额等字段先留空，识别完再补。

    thumb_sha 是聊天窗口里那张缩略图的指纹，用于【下次点开前】就判出重复。
    """
    now = created_at or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO orders
              (created_at, group_name, sender, image_path, image_sha256,
               ocr_total_cents, discount_rate, final_cents, status, note,
               thumb_sha, updated_at)
            VALUES (?,?,?,?,?,NULL,0.95,0,'processing',?,?,?)
            """,
            (now, group_name, sender, image_path, image_sha256, note,
             thumb_sha, now),
        )
        return cur.lastrowid


def update_order(order_id, **fields):
    """按字段更新。只允许白名单字段，避免拼 SQL 出错。"""
    allowed = {
        "sender", "ocr_total_cents", "discount_rate", "final_cents",
        "min_confidence", "status", "at_result", "elapsed_ms",
        "detail_check", "ocr_text", "model_json", "note",
        "reply_image_fp", "thumb_sha", "text_sent", "image_sent",
    }
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k}=?")
        params.append(v)

    if not sets:
        return False

    sets.append("updated_at=?")
    params.append(datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
    params.append(order_id)

    with connect() as conn:
        conn.execute(f"UPDATE orders SET {', '.join(sets)} WHERE id=?", params)
    return True


def mark_replied(order_id, status="auto_sent", note=None):
    """标记为已回复。replied=1 + 记录回复时间。"""
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    with connect() as conn:
        conn.execute(
            "UPDATE orders SET status=?, replied=1, replied_at=?, "
            "note=COALESCE(?, note), updated_at=? WHERE id=?",
            (status, now, note, now, order_id),
        )


def delete_order(order_id):
    """删掉一条订单（用于清理卡在「识别中」的僵尸单）。"""
    with connect() as conn:
        conn.execute("DELETE FROM orders WHERE id=?", (order_id,))


def mark_status(order_id, status, note=None):
    """只改状态，不改 replied（比如转人工、失败）。"""
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    with connect() as conn:
        conn.execute(
            "UPDATE orders SET status=?, note=COALESCE(?, note), updated_at=? WHERE id=?",
            (status, note, now, order_id),
        )
