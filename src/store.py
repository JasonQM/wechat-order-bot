"""
订单库适配层。

数据库逻辑只有一份——在 web/db.py，Web 面板和机器人共用。
这里只是把路径打通再转发，避免出现「机器人写一张表、面板读另一张表」的分裂。
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE_DIR)
WEB_DIR = os.path.join(ROOT, "web")

if WEB_DIR not in sys.path:
    sys.path.insert(0, WEB_DIR)

import db as _db  # noqa: E402

DB_PATH = _db.DB_PATH
IMAGE_DIR = _db.IMAGE_DIR

init_db = _db.init_db
insert_order = _db.insert_order
find_by_sha = _db.find_by_sha
find_by_thumb = _db.find_by_thumb
add_seen_thumb = _db.add_seen_thumb
prune_seen_thumbs = _db.prune_seen_thumbs
THUMB_MAX_DIST = _db.THUMB_MAX_DIST
set_status = _db.set_status
create_order = _db.create_order
update_order = _db.update_order
mark_replied = _db.mark_replied
mark_status = _db.mark_status
delete_order = _db.delete_order
fetch_order = _db.fetch_order
STATUS_LABELS = _db.STATUS_LABELS
