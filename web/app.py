"""
订单数据面板 · Flask 应用

启动：
    python web/app.py
    # 或指定端口
    python web/app.py --port 8080

说明
----
- 只读展示 + 人工放行操作，不直接发消息（发送由机器人主程序负责）。
- 图片从 data/images/ 直接提供，路径存相对 web 根的形式。
"""

import argparse
import os
import subprocess
import sys
import time

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, send_from_directory, url_for)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db            # noqa: E402
import groups_admin as ga  # noqa: E402
import health  # noqa: E402
import assets_admin as aa  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)

# 面板要查实例锁（判断机器人是不是已经在跑），锁的实现放在 src/ 下。
if os.path.join(ROOT_DIR, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT_DIR, "src"))
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"),
            static_folder=os.path.join(BASE_DIR, "static"))
app.secret_key = "wechat-order-panel-local"
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 上传上限 10MB

app.jinja_env.globals["STATUS_LABELS"] = db.STATUS_LABELS
app.jinja_env.globals["STATUS_TONE"] = db.STATUS_TONE
app.jinja_env.globals["yuan"] = db.yuan


@app.template_filter("dt")
def fmt_dt(value):
    """2026-11-01T09:32:11 → 11-01 09:32"""
    if not value:
        return "-"
    try:
        return value[5:16].replace("T", " ")
    except Exception:
        return value


@app.template_filter("pct")
def fmt_pct(value):
    if value is None:
        return "-"
    return f"{value * 100:.0f}%"


# ---------------------------------------------------------------- 页面
@app.route("/")
def dashboard():
    days = int(request.args.get("days", 14))
    kpi = db.fetch_kpi()
    series = db.fetch_daily_series(days)
    groups = db.fetch_group_dist(days)
    statuses = db.fetch_status_dist(days)
    recent, _ = db.fetch_orders(page=1, page_size=8)
    return render_template(
        "dashboard.html",
        active="dashboard", days=days,
        kpi=kpi, series=series, groups=groups, statuses=statuses, recent=recent,
        bot=health.status(),
    )


@app.route("/api/bot/pause", methods=["POST"])
def api_bot_pause():
    """
    暂停监控：写一个标志文件，机器人每轮自己检查。
    不用杀进程——杀掉的话恢复时要重新加载 OCR 模型（十几秒），
    而且进程一死，面板上也看不出「是暂停了还是崩了」。
    """
    try:
        os.makedirs(os.path.join(ROOT_DIR, "data"), exist_ok=True)
        with open(health.PAUSE_FLAG, "w", encoding="utf-8") as f:
            f.write("paused")
        return jsonify({"ok": True, "state": "paused", "msg": "已暂停，不再扫描和回复"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"暂停失败：{e}"}), 500


@app.route("/api/bot/resume", methods=["POST"])
def api_bot_resume():
    try:
        if os.path.isfile(health.PAUSE_FLAG):
            os.remove(health.PAUSE_FLAG)
        return jsonify({"ok": True, "state": "running", "msg": "已恢复监控"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"恢复失败：{e}"}), 500


@app.route("/api/bot/start", methods=["POST"])
def api_bot_start():
    """
    启动常驻进程。

    用 DETACHED_PROCESS 让它独立于面板进程：否则关掉面板，机器人会跟着一起死。

    启动前先查实例锁，而不是只看心跳。心跳只能说明"刚才还活着"，
    进程卡死、被挂起、或者心跳文件时间戳过期时，光看心跳会误判成"已停止"，
    于是又拉起一个——两个进程同时点开图片、同时往同一个群发报价。
    实例锁是内核对象，进程在就占着，判断才准。子进程自己也有一把锁兜底。
    """
    st = health.status()
    if st["state"] in ("running", "paused", "slow"):
        return jsonify({"ok": False, "msg": f"已经在运行了（{st['label']}）"})

    try:
        import single_instance
        if single_instance.is_taken():
            return jsonify({
                "ok": False,
                "msg": f"{single_instance.describe_owner()}，不能再启动。"
                       f"如果它已经卡死，先关掉那个窗口再试。",
            })
    except Exception:
        pass

    main_py = os.path.join(ROOT_DIR, "src", "main.py")
    if not os.path.isfile(main_py):
        return jsonify({"ok": False, "msg": "找不到 src/main.py"}, 500)

    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

    try:
        subprocess.Popen(
            [sys.executable, main_py, "loop", "--send", "--interval", "1"],
            cwd=ROOT_DIR, creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        return jsonify({"ok": False, "msg": f"启动失败：{e}"}), 500

    return jsonify({"ok": True, "msg": "启动命令已发出，几秒后刷新可看到状态"})


@app.route("/api/bot/stop", methods=["POST"])
def api_bot_stop():
    """
    真正停掉常驻进程，不只是暂停。

    「暂停」是把扫描停下来、进程还留着（恢复快，不用重新加载 OCR 模型）；
    「停止」是让进程退出、释放实例锁，下次「启动」才是干净的一轮。

    做法是写一个 stop.flag 让机器人自己退出，不是照 pid 去 taskkill：
    instance.json 里的 pid 在进程被强杀时会残留，照着旧 pid 杀，
    很可能杀掉一个复用了这个 pid 的无关程序（比如你的浏览器）。
    自己退还能顺手清掉心跳、暂停标志、持有者信息。

    为了不让用户干等，这里最多等 6 秒看锁有没有释放，
    机器人那边的长睡眠已经切成一秒一段，所以通常一两秒就有反应。
    """
    try:
        import single_instance
        alive = single_instance.is_taken()
    except Exception:
        alive = True

    if not alive:
        # 已经没在跑了：顺手把残留标志清掉，免得下次启动立刻退出
        for name in ("stop.flag", "pause.flag"):
            try:
                os.remove(os.path.join(ROOT_DIR, "data", name))
            except Exception:
                pass
        return jsonify({"ok": True, "state": "stopped", "msg": "机器人本来就没在运行"})

    try:
        os.makedirs(os.path.join(ROOT_DIR, "data"), exist_ok=True)
        with open(os.path.join(ROOT_DIR, "data", "stop.flag"), "w", encoding="utf-8") as f:
            f.write("stop")
    except Exception as e:
        return jsonify({"ok": False, "msg": f"写停止标志失败：{e}"}), 500

    deadline = time.time() + 6.0
    while time.time() < deadline:
        try:
            import single_instance
            if not single_instance.is_taken():
                return jsonify({"ok": True, "state": "stopped", "msg": "已停止"})
        except Exception:
            pass
        time.sleep(0.4)

    return jsonify({
        "ok": True, "state": "stopping",
        "msg": "停止指令已发出，进程正在退出（最多几秒），稍后刷新再看",
    })


@app.route("/api/health")
def api_health():
    """给页面轮询用的轻量状态接口。"""
    return jsonify(health.status())


@app.route("/orders")
def orders():
    date_from = request.args.get("from") or ""
    date_to = request.args.get("to") or ""
    group = request.args.get("group") or ""
    status = request.args.get("status") or ""
    keyword = request.args.get("q") or ""
    page = max(1, int(request.args.get("page", 1)))
    page_size = 20

    rows, total = db.fetch_orders(date_from, date_to, group, status, keyword, page, page_size)
    pages = max(1, (total + page_size - 1) // page_size)

    return render_template(
        "orders.html",
        active="orders",
        rows=rows, total=total, page=page, pages=pages,
        groups=db.fetch_groups(),
        f={"from": date_from, "to": date_to, "group": group, "status": status, "q": keyword},
    )


@app.route("/manual")
def manual():
    rows = db.fetch_pending()
    return render_template("manual.html", active="manual", rows=rows)


# ---------------------------------------------------------------- 群管理
@app.route("/groups")
def groups_page():
    conf = ga.load()
    return render_template(
        "groups.html",
        active="groups",
        conf=conf,
        modes=ga.MODES,
        exclude=",".join(conf.get("exclude_keywords") or []),
        test_title=request.args.get("test", ""),
        test_result=(ga.test_match(request.args.get("test", ""))
                     if request.args.get("test") else None),
    )


@app.post("/groups/add")
def groups_add():
    ok, msg = ga.add_group(request.form.get("name"),
                           request.form.get("alias"),
                           request.form.get("note"))
    flash(msg, "ok" if ok else "bad")
    return redirect(url_for("groups_page"))


@app.post("/groups/add_many")
def groups_add_many():
    ok, msg = ga.add_many(request.form.get("bulk"))
    flash(msg, "ok" if ok else "bad")
    return redirect(url_for("groups_page"))


@app.post("/groups/update/<gid>")
def groups_update(gid):
    ok, msg = ga.update_group(
        gid,
        name=request.form.get("name"),
        alias=request.form.get("alias"),
        note=request.form.get("note"),
    )
    flash(msg, "ok" if ok else "bad")
    return redirect(url_for("groups_page"))


@app.post("/groups/toggle/<gid>")
def groups_toggle(gid):
    ok, msg = ga.toggle_group(gid)
    flash(msg, "ok" if ok else "bad")
    return redirect(url_for("groups_page"))


@app.post("/groups/delete/<gid>")
def groups_delete(gid):
    ok, msg = ga.delete_group(gid)
    flash(msg, "ok" if ok else "bad")
    return redirect(url_for("groups_page"))


@app.post("/groups/settings")
def groups_settings():
    ok, msg = ga.set_settings(
        mode=request.form.get("mode"),
        exclude_keywords=request.form.get("exclude_keywords", ""),
        allow_contains=(request.form.get("allow_contains") == "on"),
    )
    flash(msg, "ok" if ok else "bad")
    return redirect(url_for("groups_page"))


@app.route("/groups/test")
def groups_test():
    return redirect(url_for("groups_page", test=request.args.get("title", "")))


# ---------------------------------------------------------------- 回复设置
@app.route("/reply")
def reply_page():
    conf = aa.load_conf()
    # 补上 pricing 需要的默认值，避免模板里 KeyError
    conf.setdefault("reply", {})
    conf["reply"].setdefault("template", "{name}，订单合计 {total} 元，95 折后 {final} 元")
    conf["reply"].setdefault("discount_label", "95 折")
    conf["reply"].setdefault("mention", True)
    conf.setdefault("discount_percent", 95)

    return render_template(
        "reply.html",
        active="reply",
        conf=conf,
        img=aa.current_image(),
        preview=aa.preview_text(),
    )


@app.post("/reply/upload")
def reply_upload():
    f = request.files.get("file")
    ok, msg, _info = aa.save_upload(f)
    flash(msg, "ok" if ok else "bad")
    return redirect(url_for("reply_page"))


@app.post("/reply/save")
def reply_save():
    ok, msg = aa.set_reply(
        template=request.form.get("template"),
        discount_label=request.form.get("discount_label"),
        mention=(request.form.get("mention") == "on"),
        discount_percent=request.form.get("discount_percent"),
    )
    flash(msg, "ok" if ok else "bad")
    return redirect(url_for("reply_page"))


@app.route("/assets/<path:filename>")
def assets_file(filename):
    return send_from_directory(aa.ASSETS_DIR, filename)


# ---------------------------------------------------------------- API
@app.route("/api/stats")
def api_stats():
    days = int(request.args.get("days", 14))
    return jsonify({
        "kpi": db.fetch_kpi(),
        "series": db.fetch_daily_series(days),
        "groups": db.fetch_group_dist(days),
        "statuses": db.fetch_status_dist(days),
    })


@app.post("/api/orders/<int:order_id>/approve")
def api_approve(order_id):
    """
    把一单交回给机器人重新处理。

    这里原来只有一句 set_status(..., "manual_sent", "面板人工放行")。
    问题很严重：**面板没有任何操作微信窗口的能力，那条消息根本没发出去**。
    而 manual_sent 又会让机器人在点开前判成"已处理"、永久跳过这一天。
    结果就是面板显示「人工放行」，客户手机上一片安静，谁也发现不了。
    （实测订单 #16 就是这么变成 final_cents=0、replied=0、状态却是 manual_sent 的。）

    现在改成真正的"重试"：
      · 先清掉这处缩略图观察记录，否则下一轮它在点开前就被跳过了
      · 再删掉这条订单，机器人会当成新图重新抓取、重新识别；
        识别通过才会发送，不通过还是转人工，不会瞎报一个价
    """
    order = db.fetch_order(order_id)
    if not order:
        abort(404)
    if order["replied"]:
        msg = "这单已经回复过了，不能重复发"
        if request.is_json:
            return jsonify({"ok": False, "msg": msg}), 400
        flash(msg, "bad")
        return redirect(url_for("manual"))

    try:
        db.forget_thumb(order_id)
        db.delete_order(order_id)
    except Exception as e:
        msg = f"重置失败：{e}"
        if request.is_json:
            return jsonify({"ok": False, "msg": msg}), 500
        flash(msg, "bad")
        return redirect(url_for("manual"))

    msg = "已交回机器人重新识别，下一轮扫描时会重新取图"
    if request.is_json:
        return jsonify({"ok": True, "id": order_id, "status": "retry"})
    flash(msg, "ok")
    return redirect(url_for("manual"))


@app.post("/api/orders/<int:order_id>/reject")
def api_reject(order_id):
    if not db.fetch_order(order_id):
        abort(404)
    db.set_status(order_id, "ignored", "面板人工忽略")
    if request.is_json:
        return jsonify({"ok": True, "id": order_id, "status": "ignored"})
    return redirect(url_for("manual"))


@app.post("/api/orders/<int:order_id>/note")
def api_note(order_id):
    if not db.fetch_order(order_id):
        abort(404)
    note = (request.form.get("note") or "").strip()
    db.set_status(order_id, db.fetch_order(order_id)["status"], note)
    return redirect(request.referrer or url_for("orders"))


@app.route("/images/<path:filename>")
def images(filename):
    return send_from_directory(db.IMAGE_DIR, filename)


@app.template_filter("basename")
def basename(path):
    return os.path.basename(path) if path else ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    db.init_db()
    print(f"数据库: {db.DB_PATH}")
    print(f"面板地址: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)


if __name__ == "__main__":
    main()
