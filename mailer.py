# -*- coding: utf-8 -*-
"""每晚推送：明日课表 + 未完成待办，经 PushPlus 发到微信；支持开机补发。"""
import json
import logging
import time
import urllib.request
from datetime import date, datetime, timedelta

import clawbot
import common
import db
import logic

log = logging.getLogger("workbench")

WD = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _remaining(due: str) -> str:
    """距截止的剩余时间描述，如『还剩 3 天』『已逾期 2 小时』。"""
    try:
        dt = datetime.strptime(due, "%Y-%m-%d %H:%M")
    except ValueError:
        return ""
    diff = dt - datetime.now()
    secs = abs(int(diff.total_seconds()))
    if secs >= 86400:
        txt = f"{secs // 86400} 天"
    elif secs >= 3600:
        txt = f"{secs // 3600} 小时"
    else:
        txt = f"{max(secs // 60, 1)} 分钟"
    return ("已逾期 " if diff.total_seconds() < 0 else "还剩 ") + txt


def build_digest(target: date) -> tuple:
    """返回 (subject, html)。"""
    info = logic.day_items(target)
    todos = [t for t in logic.todos_snapshot() if not t["done"]]
    overdue = [t for t in todos if t["due"] and t["due"] < datetime.now().strftime("%Y-%m-%d %H:%M")]

    wd = WD[target.weekday()]
    week_part = f"{info['term']}第 {info['week']} 周" if info["term"] else "假期中"
    if info["holiday"]:
        brief = "假期"
    else:
        n_items = len(info["items"])
        brief = f"{n_items} 项日程" if n_items else "无日程"
    subject = (f"【叮咛】明天 {target.month}/{target.day} {wd} · "
               f"{brief} · 待办 {len(todos)}")

    rows_html = ""
    for it in info["items"]:
        time_txt = it["start"] if it["start"] else "全天"
        if it["end"]:
            time_txt += f"–{it['end']}"
        loc = f"<span class='loc'>{_esc(it['location'])}</span>" if it["location"] else ""
        rows_html += (f"<tr><td class='t'>{time_txt}</td>"
                      f"<td><b>{_esc(it['title'])}</b> {loc}</td></tr>")
    if not rows_html:
        rows_html = "<tr><td class='empty'>明天没有课程或日程安排 🎉</td></tr>"

    banners = ""
    for b in info["banners"]:
        t = f"{b['time']} " if b["time"] else ""
        banners += f"<div class='banner {b['kind']}'>【{b['label']}】{t}{_esc(b['title'])}</div>"
    if info["holiday"]:
        banners += "<div class='banner holiday'><b>放假提示：明天假期停课。</b></div>"

    todo_html = ""
    for t in todos:
        cls = "overdue" if t in overdue else ("today" if (t["due"] or "").startswith(date.today().isoformat()) else "")
        due = f"<span class='due'>{t['due']}（{_remaining(t['due'])}）</span>" if t["due"] else ""
        todo_html += f"<li class='{cls}'>{_esc(t['title'])} {due}</li>"
    if not todo_html:
        todo_html = "<li class='empty'>没有未完成的待办 </li>"

    html = f"""<div style="background:#f4f6fb;padding:20px 8px;">
  <div style="font-family:'Microsoft YaHei',sans-serif;max-width:640px;margin:0 auto;color:#1f2937;background:#ffffff;border-radius:14px;padding:24px 28px;">
  <h2 style="border-bottom:3px solid #2563eb;padding-bottom:8px;color:#1f2937;">📅 明天 · {target.month}月{target.day}日 {wd} · {week_part}</h2>
  {banners}
  <h3 style="color:#1f2937;">日程 / 课表</h3>
  <table style="border-collapse:collapse;width:100%;">{rows_html}</table>
  <h3 style="color:#1f2937;">📋 待办（{len(todos)} 项未完成{'，其中逾期 ' + str(len(overdue)) + ' 项' if overdue else ''}）</h3>
  <ul style="padding-left:20px;">{todo_html}</ul>
  <p style="color:#9ca3af;font-size:12px;margin-top:24px;">由叮咛自动发送 · 发送时间 {datetime.now().strftime('%H:%M')}</p>
</div></div>"""
    style = """<style>
      table td{padding:6px 10px;border-bottom:1px solid #eee;vertical-align:top;}
      td.t{color:#2563eb;white-space:nowrap;font-weight:600;width:110px;}
      .loc{color:#6b7280;font-size:13px;margin-left:6px;}
      .banner{padding:8px 12px;border-radius:8px;margin:6px 0;font-size:14px;}
      .banner.holiday{background:#fef3c7;} .banner.festival{background:#fce7f3;}
      .banner.exam{background:#fee2e2;} .banner.event{background:#e0e7ff;} .banner.week_note{background:#f3f4f6;}
      li{margin:4px 0;} li.overdue{color:#dc2626;font-weight:600;} li.today .due{color:#ea580c;}
      .due{color:#6b7280;font-size:13px;margin-left:6px;} .empty{color:#9ca3af;}
    </style>"""
    return subject, style + html


def _send_pushplus_once(title: str, html: str):
    """单次发送。返回 (ok, detail)。"""
    token = db.get_config("pushplus_token")
    if not token:
        return False, "token 未配置"
    try:
        body = json.dumps({"token": token, "title": title,
                           "content": html, "template": "html"}).encode("utf-8")
        req = urllib.request.Request("http://www.pushplus.plus/send", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=25) as r:
            resp = json.loads(r.read().decode("utf-8"))
        if resp.get("code") == 200:
            log.info("PushPlus 已发送: %s", title)
            return True, ""
        return False, f"服务端返回 {resp}"
    except Exception as e:
        return False, repr(e)[:120]


def send_push(title: str, html: str, retries: int = 3) -> bool:
    """统一推送入口：WxPusher（APP）优先，未配置或失败则回退 PushPlus。"""
    import wxpusher
    if wxpusher.configured():
        import time as _time
        uid = (db.get_config("wxpusher_uid") or "").strip()
        for i in range(2):
            ok, detail = wxpusher.send(uid, title, html)
            if ok:
                log.info("WxPusher 已发送: %s", title)
                return True
            log.warning("WxPusher 第 %d 次发送失败（%s）", i + 1, detail)
            _time.sleep(3)
        log.error("WxPusher 仍失败，转 PushPlus 兜底（%s）", title)
    return send_pushplus(title, html)                     # 重试次数走默认（3）


def send_pushplus(title: str, html: str, retries: int = 3) -> bool:
    """带重试的推送：网络瞬断时 5s/10s 退避重试，避免一次失败整天收不到。"""
    import time as _time
    for i in range(retries):
        ok, detail = _send_pushplus_once(title, html)
        if ok:
            return True
        if i < retries - 1:
            delay = 5 * (i + 1)
            log.warning("PushPlus 第 %d 次发送失败（%s），%ds 后重试", i + 1, detail, delay)
            time.sleep(delay)
    log.error("PushPlus 发送失败：重试 %d 次仍未成功（%s）", retries, title)
    return False


_watch_attempts = set()   # 看门狗当天已尝试过的任务（防止同一任务反复触发）


def _run_per_user(fn):
    """遍历所有用户，逐个把 ContextVar 切到该用户业务库后执行 fn()。

    fn 返回真值视为"该用户有动作"；未配置 token 等情况由 fn 自行跳过。
    """
    done = []
    for name in db.all_usernames():
        db.set_current_user(name)
        try:
            if fn():
                done.append(name)
        except Exception as e:
            log.warning("用户 %s 后台任务异常：%r", name, e)
    return done


def _push_passed(pt: str, now) -> bool:
    """推送时刻是否已过（now 为 datetime）。"""
    try:
        h, m = map(int, (pt or "21:00").split(":"))
    except ValueError:
        h, m = 21, 0
    return now.hour > h or (now.hour == h and now.minute >= m)


def _sent_today() -> bool:
    return db.get_config("last_digest_date") == datetime.now().date().isoformat()


def _nightly_digest():
    """每分钟巡检：命中某用户自己配置的推送时刻（HH:MM 相等）就发该用户的明日摘要。"""
    hhmm = datetime.now().strftime("%H:%M")
    return _run_per_user(lambda: db.get_config("push_time", "21:00") == hhmm
                         and not _sent_today() and bool(digest_job()))


def _watchdog():
    """每 10 分钟巡检：推送时刻已过且今天没发过 → 补发（兜底重启补发之外的自愈）。"""
    now = datetime.now()
    today = now.date().isoformat()

    def job():
        if (_push_passed(db.get_config("push_time", "21:00"), now)
                and not _sent_today() and f"digest:{today}" not in _watch_attempts):
            _watch_attempts.add(f"digest:{today}")
            log.info("看门狗：今天的摘要尚未发送，补发")
            digest_job()
            return True
        return False

    return _run_per_user(job)


# ---------------- 分钟级提醒（广义日程：日程 / 待办 / 课） ----------------

_REMIND_FRESH_MIN = 30   # 提醒时刻已过超过 30 分钟视为过期（停机期间不补轰炸）

def _remind_due(c, key, remind_at, now) -> bool:
    """已发过 → False；未到点 → False；到点（含新鲜窗内停机补发）→ True。"""
    if c.execute("SELECT 1 FROM reminders_sent WHERE rkey=?", (key,)).fetchone():
        return False
    if now < remind_at:
        return False
    if now - remind_at > timedelta(minutes=_REMIND_FRESH_MIN):
        c.execute("INSERT OR REPLACE INTO reminders_sent(rkey, sent_at) VALUES(?,?)",
                  (key, now.strftime("%Y-%m-%d %H:%M:%S")))
        return False
    return True

def _mark_reminded(c, key, now):
    c.execute("INSERT OR REPLACE INTO reminders_sent(rkey, sent_at) VALUES(?,?)",
              (key, now.strftime("%Y-%m-%d %H:%M:%S")))

def _remind_scan_one() -> int:
    """扫描当前用户的到期提醒并推送。返回条数。"""
    now = datetime.now()
    today = now.date().isoformat()
    n = 0
    with db.conn() as c:
        # 1) 日程（有时刻且设了提醒；全天日程暂不支持提前提醒）
        evs = c.execute("SELECT id, title, start_time, remind_min, location FROM events "
                        "WHERE date=? AND source='user' AND remind_min IS NOT NULL "
                        "AND start_time!=''", (today,)).fetchall()
        for r in evs:
            try:
                start = datetime.fromisoformat(f"{today}T{r['start_time']}:00")
            except ValueError:
                continue
            remind_at = start - timedelta(minutes=r["remind_min"])
            adj = common.ring_time_adjust(now.date(), remind_at.strftime("%H:%M"))
            if adj != remind_at.strftime("%H:%M"):
                remind_at = datetime.combine(                 # 撞课：挪到课前 5 分钟
                    remind_at.date(), datetime.strptime(adj, "%H:%M").time())
                log.info("日程提醒撞课已调整：%s %s -> %s", r["title"][:20],
                         start.strftime("%H:%M"), adj)
            key = f"event:{r['id']}:{r['remind_min']}:{r['start_time']}"
            if not _remind_due(c, key, remind_at, now):
                continue
            mm = r["remind_min"]
            when = "现在开始" if mm == 0 else f"{mm} 分钟后开始"
            ok = send_push(f"叮咛 · {when}: {r['title'][:40]}",
                           f"<p>{r['start_time']} 开始：{r['title']}"
                           + (f"<br>地点：{r['location']}" if r["location"] else "") + "</p>")
            if ok:
                _mark_reminded(c, key, now); n += 1
        # 2) 待办（设了提醒、有截止时刻）
        todos = c.execute("SELECT id, title, due, remind_min FROM todos "
                          "WHERE remind_min IS NOT NULL AND remind_min >= 0 AND due != ''").fetchall()
        for r in todos:
            try:
                due_dt = datetime.fromisoformat(
                    r["due"] if len(r["due"]) > 10 else r["due"] + " 00:00")
            except ValueError:
                continue
            remind_at = due_dt - timedelta(minutes=r["remind_min"])
            if remind_at.date() != today:
                continue                                 # 只扫今天该发的
            adj = common.ring_time_adjust(now.date(), remind_at.strftime("%H:%M"))
            if adj != remind_at.strftime("%H:%M"):
                remind_at = datetime.combine(                 # 撞课：挪到课前 5 分钟
                    remind_at.date(), datetime.strptime(adj, "%H:%M").time())
            key = f"todo:{r['id']}:{r['remind_min']}:{r['due']}"
            if not _remind_due(c, key, remind_at, now):
                continue
            mm = r["remind_min"]
            when = "现在截止" if mm == 0 else f"{mm} 分钟后截止"
            ok = send_push(f"叮咛 · 待办{when}: {r['title'][:30]}",
                           f"<p>截止 {r['due'][5:16]}：{r['title']}</p>")
            if ok:
                _mark_reminded(c, key, now); n += 1
        # 3) 课（全局设置：每节课开始前 N 分钟）
        lmin_s = (db.get_config("lesson_remind_min") or "").strip()
        lmin = int(lmin_s) if lmin_s.isdigit() and int(lmin_s) > 0 else None
        if not lmin:
            return n
        info = logic.day_items(now.date(), now.date())
        for it in info["items"]:
            if it.get("type") != "lesson" or it.get("cancelled") or not it.get("start"):
                continue
            try:
                start = datetime.fromisoformat(f"{today}T{it['start']}:00")
            except ValueError:
                continue
            remind_at = start - timedelta(minutes=lmin)
            key = f"lesson:{today}:{it['section']}:{it.get('class_name','')}:{lmin}"
            if not _remind_due(c, key, remind_at, now):
                continue
            ok = send_push(f"叮咛 · {lmin} 分钟后上课: {it['title'][:30]}",
                           f"<p>{it['start']} {it['section']}：{it['title']}</p>")
            if ok:
                _mark_reminded(c, key, now); n += 1
    return n


def digest_job(target: date = None) -> bool:
    """推送当前用户的摘要并记录日期（用于补发判断）。"""
    target = target or (date.today() + timedelta(days=1))
    subject, html = build_digest(target)
    ok = send_push(subject, html)
    if ok:
        db.set_config("last_digest_date", date.today().isoformat())
    return ok


def catchup():
    """启动补发（逐用户）：错过推送时刻则补上。"""
    now = datetime.now()
    today = now.date().isoformat()

    def job():
        last = db.get_config("last_digest_date")
        pt = db.get_config("push_time", "21:00")
        if _push_passed(pt, now) and last != today:
            log.info("启动补发：今天 %s 尚未推送，补发明日摘要", pt)
            return digest_job()
        if not _push_passed(pt, now) and last not in (
                "", today, (now.date() - timedelta(days=1)).isoformat()):
            log.info("启动补发：昨晚摘要缺失，补发今日摘要")
            return digest_job(date.today())
        return False

    return _run_per_user(job)


def _remind_poll():
    """分钟级提醒扫描（逐用户：各自的科目/待办/通道）。"""
    def job():
        try:
            n = _remind_scan_one()
            if n:
                log.info("提醒已推送 %d 条", n)
        except Exception as e:
            log.warning("提醒扫描异常：%r", e)
        return True
    return _run_per_user(job)


def _clawbot_poll():
    """轮询微信 ClawBot 收到的文字指令并回执（逐用户，未配置的用户静默跳过）。"""

    def job():
        if not clawbot.enabled():
            return False
        try:
            n = clawbot.poll_once()
            if n:
                log.info("ClawBot 处理并回复 %d 条消息", n)
            return True
        except Exception as e:
            log.warning("ClawBot 轮询异常：%r", e)
            return False

    return _run_per_user(job)


_sched = None


def setup_scheduler():
    global _sched
    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler(daemon=True, timezone="Asia/Shanghai")
    # 推送时间按用户各自配置（config.push_time），故每分钟巡检、命中各自时刻才发
    sched.add_job(_nightly_digest, "cron", minute="*", id="nightly_digest")
    sched.add_job(_watchdog, "interval", minutes=10, id="push_watchdog")   # 漏发巡检（网络瞬断兜底）
    sched.add_job(_clawbot_poll, "interval", seconds=10, id="clawbot_poll")  # 微信指令收件箱
    sched.add_job(_remind_poll, "interval", seconds=60, id="remind_scan")     # 分钟级提醒扫描
    sched.start()
    _sched = sched
    log.info("定时推送已启动（按各用户配置的推送时刻）")
    return sched
