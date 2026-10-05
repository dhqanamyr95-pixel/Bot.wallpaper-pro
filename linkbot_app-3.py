import os
import sqlite3
import secrets
import threading
import time

import requests
from flask import Flask, request

import linkbot_config as config

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "linkbot.db")
API = "https://api.telegram.org/bot%s/" % config.BOT_TOKEN

app = Flask(__name__)

INVALID = "❌ لینک نامعتبر است.\nلطفاً لینک درست را بفرستید."

ADMIN_CMDS = [
    {"command": "new", "description": "ساخت لینک جدید"},
    {"command": "done", "description": "پایان و دریافت لینک"},
    {"command": "cancel", "description": "لغو عملیات"},
    {"command": "broadcast", "description": "پیام همگانی"},
    {"command": "stats", "description": "تعداد اعضا"},
]
OWNER_CMDS = ADMIN_CMDS + [
    {"command": "channels", "description": "لیست کانال‌های اجباری"},
    {"command": "addchannel", "description": "افزودن کانال اجباری"},
    {"command": "delchannel", "description": "حذف کانال اجباری"},
    {"command": "admins", "description": "لیست ادمین‌ها"},
    {"command": "addadmin", "description": "افزودن ادمین"},
    {"command": "deladmin", "description": "حذف ادمین"},
]

BTN = {
    "➕ ساخت لینک": "new",
    "✅ پایان و دریافت لینک": "done",
    "📣 پیام همگانی": "broadcast",
    "📊 آمار": "stats",
    "❌ لغو": "cancel",
    "📢 کانال‌ها": "channels",
    "➕ افزودن کانال": "addchannel_p",
    "🗑 حذف کانال": "delchannel_p",
    "👥 ادمین‌ها": "admins",
    "➕ افزودن ادمین": "addadmin_p",
    "🗑 حذف ادمین": "deladmin_p",
}

PROMPTS = {
    "addchannel_p": "آیدی کانال رو بفرست: @username یا -100...\n(ربات باید ادمین کانال باشد) | لغو: ❌ لغو",
    "delchannel_p": "شماره کانال رو بفرست (لیست: 📢 کانال‌ها) | لغو: ❌ لغو",
    "addadmin_p": "آیدی عددی ادمین جدید رو بفرست | لغو: ❌ لغو",
    "deladmin_p": "آیدی عددی ادمینی که می‌خوای حذف بشه رو بفرست | لغو: ❌ لغو",
}


def kb(uid):
    rows = [["➕ ساخت لینک", "✅ پایان و دریافت لینک"],
            ["📣 پیام همگانی", "📊 آمار"],
            ["❌ لغو"]]
    if uid == config.OWNER_ID:
        rows += [["📢 کانال‌ها", "➕ افزودن کانال"],
                 ["🗑 حذف کانال", "👥 ادمین‌ها"],
                 ["➕ افزودن ادمین", "🗑 حذف ادمین"]]
    return {"keyboard": [[{"text": t} for t in r] for r in rows],
            "resize_keyboard": True, "is_persistent": True}


SEND_METHODS = {
    "photo": ("sendPhoto", "photo"),
    "video": ("sendVideo", "video"),
    "animation": ("sendAnimation", "animation"),
    "document": ("sendDocument", "document"),
    "audio": ("sendAudio", "audio"),
}


# ---------------- دیتابیس ----------------
def db():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = db()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS channels(chat_id TEXT PRIMARY KEY, title TEXT, link TEXT);
        CREATE TABLE IF NOT EXISTS links(code TEXT PRIMARY KEY, created_by INTEGER);
        CREATE TABLE IF NOT EXISTS items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT, kind TEXT, file_id TEXT, caption TEXT);
        CREATE TABLE IF NOT EXISTS state(
            user_id INTEGER PRIMARY KEY, mode TEXT, last_group TEXT);
        """
    )
    for col in ("joined INTEGER", "opens INTEGER DEFAULT 0"):
        try:
            c.execute("ALTER TABLE users ADD COLUMN " + col)
        except sqlite3.OperationalError:
            pass  # ستون از قبل هست
    c.commit()
    c.close()


init_db()


def register_user(uid):
    # هر نفر فقط یک بار ثبت می‌شود (کلید یکتا)
    q("INSERT OR IGNORE INTO users(id, joined, opens) VALUES(?,?,0)",
      (uid, int(time.time())), commit=True)


def stats_text():
    ex = " id != ? AND id NOT IN (SELECT id FROM admins)"
    o = config.OWNER_ID
    now = int(time.time())

    def cnt(extra=""):
        return q("SELECT COUNT(*) c FROM users WHERE" + ex + extra, (o,), one=True)["c"]

    total = cnt()
    today = q("SELECT COUNT(*) c FROM users WHERE" + ex + " AND joined > ?", (o, now - 86400), one=True)["c"]
    week = q("SELECT COUNT(*) c FROM users WHERE" + ex + " AND joined > ?", (o, now - 7 * 86400), one=True)["c"]
    got = cnt(" AND COALESCE(opens,0) > 0")
    opens = q("SELECT COALESCE(SUM(opens),0) s FROM users WHERE" + ex, (o,), one=True)["s"]
    return ("📊 آمار ربات (بدون احتساب ادمین‌ها)\n\n"
            "👥 کل کاربران (هر نفر یک بار): %d\n"
            "🆕 امروز (۲۴ ساعت اخیر): %d\n"
            "📅 هفته اخیر: %d\n"
            "📥 کاربرانی که حداقل یک‌بار محتوا گرفته‌اند: %d\n"
            "🔗 مجموع دفعات دریافت محتوا: %d" % (total, today, week, got, opens))


def q(sql, args=(), one=False, commit=False):
    c = db()
    cur = c.execute(sql, args)
    rows = cur.fetchone() if one else cur.fetchall()
    if commit:
        c.commit()
    c.close()
    return rows


# ---------------- تلگرام ----------------
def tg(method, **params):
    try:
        return requests.post(API + method, json=params, timeout=30).json()
    except Exception:
        return {"ok": False}


def send(chat, text, **kw):
    return tg("sendMessage", chat_id=chat, text=text, **kw)


def is_admin(uid):
    if uid == config.OWNER_ID:
        return True
    return q("SELECT 1 FROM admins WHERE id=?", (uid,), one=True) is not None


def set_cmds(uid):
    cmds = OWNER_CMDS if uid == config.OWNER_ID else ADMIN_CMDS
    tg("setMyCommands", commands=cmds, scope={"type": "chat", "chat_id": uid})


def get_state(uid):
    r = q("SELECT mode, last_group FROM state WHERE user_id=?", (uid,), one=True)
    return (r["mode"], r["last_group"]) if r else (None, None)


def set_state(uid, mode, last_group=None):
    if mode is None:
        q("DELETE FROM state WHERE user_id=?", (uid,), commit=True)
    else:
        q("INSERT OR REPLACE INTO state(user_id, mode, last_group) VALUES(?,?,?)",
          (uid, mode, last_group), commit=True)


def chat_ref(v):
    v = str(v)
    return int(v) if v.lstrip("-").isdigit() else v


# ---------------- عضویت اجباری ----------------
def not_joined(uid):
    missing = []
    for ch in q("SELECT * FROM channels"):
        r = tg("getChatMember", chat_id=chat_ref(ch["chat_id"]), user_id=uid)
        if not r.get("ok"):
            continue  # ربات ادمین کانال نیست؛ این کانال رد می‌شود
        res = r["result"]
        st = res.get("status")
        if st in ("left", "kicked") or (st == "restricted" and not res.get("is_member")):
            missing.append(ch)
    return missing


def join_message(chat, code, missing):
    rows = [[{"text": "📢 " + (ch["title"] or "کانال"), "url": ch["link"]}] for ch in missing]
    rows.append([{"text": "✅ عضو شدم", "callback_data": "chk:" + code}])
    send(chat, "برای دریافت محتوا ابتدا در کانال‌های زیر عضو شوید، سپس «عضو شدم» را بزنید 👇",
         reply_markup={"inline_keyboard": rows})


# ---------------- ارسال محتوا ----------------
def send_single(chat, kind, file_id, caption):
    method, field = SEND_METHODS[kind]
    params = {"chat_id": chat, field: file_id}
    if caption:
        params["caption"] = caption
    tg(method, **params)


def send_content(chat, code):
    items = q("SELECT kind, file_id, caption FROM items WHERE code=? ORDER BY id", (code,))
    batch = []

    def flush():
        if not batch:
            return
        if len(batch) == 1:
            send_single(chat, "photo", batch[0]["file_id"], batch[0]["caption"])
        else:
            media = []
            for it in batch:
                m = {"type": "photo", "media": it["file_id"]}
                if it["caption"]:
                    m["caption"] = it["caption"]
                media.append(m)
            tg("sendMediaGroup", chat_id=chat, media=media)
        batch.clear()
        time.sleep(0.4)

    for it in items:
        if it["kind"] == "photo":
            batch.append(it)
            if len(batch) == 10:
                flush()
        else:
            flush()
            send_single(chat, it["kind"], it["file_id"], it["caption"])
    flush()
    q("UPDATE users SET opens = COALESCE(opens, 0) + 1 WHERE id=?", (chat,), commit=True)


def deliver_flow(uid, chat, code):
    if not q("SELECT 1 FROM links WHERE code=?", (code,), one=True):
        return send(chat, INVALID, reply_markup={"remove_keyboard": True})
    if not is_admin(uid):
        missing = not_joined(uid)
        if missing:
            return join_message(chat, code, missing)
    send_content(chat, code)


# ---------------- ادمین ----------------
def extract_media(m):
    cap = m.get("caption") or ""
    if "animation" in m:
        return "animation", m["animation"]["file_id"], cap
    if "photo" in m:
        return "photo", m["photo"][-1]["file_id"], cap
    if "video" in m:
        return "video", m["video"]["file_id"], cap
    if "document" in m:
        return "document", m["document"]["file_id"], cap
    if "audio" in m:
        return "audio", m["audio"]["file_id"], cap
    return None


def do_broadcast(src_chat, msg_id, admin):
    ids = [r["id"] for r in q("SELECT id FROM users")]
    ok = fail = 0
    for u in ids:
        r = tg("copyMessage", chat_id=u, from_chat_id=src_chat, message_id=msg_id)
        if r.get("ok"):
            ok += 1
        else:
            fail += 1
        time.sleep(0.05)
    send(admin, "📣 ارسال همگانی تمام شد.\n✅ موفق: %d\n❌ ناموفق: %d" % (ok, fail))


def bot_username():
    r = tg("getMe")
    return r.get("result", {}).get("username", "")


def admin_help(uid):
    t = ("پنل ادمین 👇\n"
         "برای ساخت لینک: «➕ ساخت لینک» را بزن، فایل‌ها را بفرست، بعد «✅ پایان و دریافت لینک».")
    send(uid, t, reply_markup=kb(uid))


def admin_flow(m, uid, cmd, arg):
    owner = uid == config.OWNER_ID
    draft = "draft:%d" % uid

    if cmd in ("start", "help"):
        set_cmds(uid)
        return admin_help(uid)

    if cmd == "new":
        q("DELETE FROM items WHERE code=?", (draft,), commit=True)
        set_state(uid, "collect")
        return send(uid, "📥 حالا فایل‌ها را بفرست (عکس، ویدیو، گیف، فایل). حداکثر %d تا.\nوقتی تمام شد: /done" % config.MAX_ITEMS)

    if cmd == "done":
        n = q("SELECT COUNT(*) c FROM items WHERE code=?", (draft,), one=True)["c"]
        if n == 0:
            return send(uid, "هنوز چیزی نفرستادی. اول /new")
        code = secrets.token_hex(5)
        q("UPDATE items SET code=? WHERE code=?", (code, draft), commit=True)
        q("INSERT INTO links(code, created_by) VALUES(?,?)", (code, uid), commit=True)
        set_state(uid, None)
        return send(uid, "✅ لینک ساخته شد (%d فایل):\nhttps://t.me/%s?start=%s" % (n, bot_username(), code))

    if cmd == "cancel":
        q("DELETE FROM items WHERE code=?", (draft,), commit=True)
        set_state(uid, None)
        return send(uid, "لغو شد.")

    if cmd == "broadcast":
        set_state(uid, "broadcast")
        return send(uid, "📣 پیام همگانی رو بفرست (متن/عکس/هرچی). برای لغو: /cancel")

    if cmd == "stats":
        return send(uid, stats_text())

    # ---- دکمه‌هایی که ورودی بعدی را می‌خواهند ----
    if cmd in PROMPTS:
        if not owner:
            return send(uid, "این بخش فقط برای مالک است.")
        set_state(uid, "await_" + cmd[:-2])
        return send(uid, PROMPTS[cmd])

    # ---- فقط مالک ----
    if cmd in ("addchannel", "delchannel", "channels", "addadmin", "deladmin", "admins"):
        if not owner:
            return send(uid, "این دستور فقط برای مالک است.")
        return owner_cmds(uid, cmd, arg)

    # ---- پیام بدون دستور ----
    mode, last_group = get_state(uid)
    if mode and mode.startswith("await_") and owner:
        set_state(uid, None)
        return owner_cmds(uid, mode[6:], (m.get("text") or "").strip())
    if mode == "collect":
        media = extract_media(m)
        if not media:
            return send(uid, "فقط عکس، ویدیو، گیف یا فایل بفرست.")
        n = q("SELECT COUNT(*) c FROM items WHERE code=?", (draft,), one=True)["c"]
        if n >= config.MAX_ITEMS:
            return send(uid, "به سقف %d فایل رسیدی. /done بزن." % config.MAX_ITEMS)
        q("INSERT INTO items(code, kind, file_id, caption) VALUES(?,?,?,?)",
          (draft, media[0], media[1], media[2]), commit=True)
        gid = m.get("media_group_id")
        set_state(uid, "collect", gid)
        if not gid or gid != last_group:
            send(uid, "✅ دریافت شد. وقتی تمام شد: /done")
        return
    if mode == "broadcast":
        set_state(uid, None)
        send(uid, "⏳ در حال ارسال...")
        threading.Thread(target=do_broadcast, args=(m["chat"]["id"], m["message_id"], uid), daemon=True).start()
        return
    admin_help(uid)


def owner_cmds(uid, cmd, arg):
    if cmd == "channels":
        rows = q("SELECT * FROM channels")
        if not rows:
            return send(uid, "کانال اجباری ندارید.")
        return send(uid, "\n".join("%d) %s — %s" % (i + 1, r["title"], r["chat_id"]) for i, r in enumerate(rows)))

    if cmd == "addchannel":
        if not arg:
            return send(uid, "مثال: /addchannel @mychannel\nیا /addchannel -1001234567890\n(ربات باید ادمین کانال باشد)")
        if q("SELECT COUNT(*) c FROM channels", one=True)["c"] >= config.MAX_CHANNELS:
            return send(uid, "حداکثر %d کانال." % config.MAX_CHANNELS)
        r = tg("getChat", chat_id=chat_ref(arg))
        if not r.get("ok"):
            return send(uid, "کانال پیدا نشد. ربات را ادمین کانال کن و دوباره بزن.")
        ch = r["result"]
        if ch.get("username"):
            link = "https://t.me/" + ch["username"]
        else:
            link = tg("exportChatInviteLink", chat_id=ch["id"]).get("result")
        if not link:
            return send(uid, "لینک کانال گرفته نشد. ربات را ادمین (با دسترسی دعوت) کن.")
        q("INSERT OR REPLACE INTO channels(chat_id, title, link) VALUES(?,?,?)",
          (str(ch["id"]), ch.get("title"), link), commit=True)
        return send(uid, "✅ کانال اضافه شد: %s" % ch.get("title"))

    if cmd == "delchannel":
        rows = q("SELECT * FROM channels")
        target = None
        if arg.isdigit() and 1 <= int(arg) <= len(rows):
            target = rows[int(arg) - 1]["chat_id"]
        elif any(r["chat_id"] == arg for r in rows):
            target = arg
        if not target:
            return send(uid, "شماره یا آیدی کانال را بده. لیست: /channels")
        q("DELETE FROM channels WHERE chat_id=?", (target,), commit=True)
        return send(uid, "🗑 حذف شد.")

    if cmd == "admins":
        rows = q("SELECT id FROM admins")
        if not rows:
            return send(uid, "ادمین دیگری ندارید.")
        return send(uid, "\n".join(str(r["id"]) for r in rows))

    if cmd == "addadmin":
        if not arg.isdigit():
            return send(uid, "مثال: /addadmin 123456789")
        if q("SELECT COUNT(*) c FROM admins", one=True)["c"] >= config.MAX_ADMINS:
            return send(uid, "حداکثر %d ادمین." % config.MAX_ADMINS)
        q("INSERT OR IGNORE INTO admins(id) VALUES(?)", (int(arg),), commit=True)
        set_cmds(int(arg))
        return send(uid, "✅ ادمین اضافه شد. (باید یک‌بار /start بزند)")

    if cmd == "deladmin":
        if not arg.isdigit():
            return send(uid, "مثال: /deladmin 123456789")
        q("DELETE FROM admins WHERE id=?", (int(arg),), commit=True)
        tg("deleteMyCommands", scope={"type": "chat", "chat_id": int(arg)})
        send(int(arg), "دسترسی ادمین شما برداشته شد.", reply_markup={"remove_keyboard": True})
        return send(uid, "🗑 ادمین حذف شد.")


# ---------------- ورودی‌ها ----------------
def handle_message(m):
    if m["chat"]["type"] != "private" or "from" not in m:
        return
    uid = m["from"]["id"]
    chat = m["chat"]["id"]
    text = m.get("text") or ""
    cmd, arg = None, ""
    if text.startswith("/"):
        parts = text.split(None, 1)
        cmd = parts[0][1:].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "start":
        register_user(uid)

    if text in BTN and is_admin(uid):
        cmd, arg = BTN[text], ""

    if cmd == "start" and arg:
        return deliver_flow(uid, chat, arg)

    if not is_admin(uid):
        return send(chat, INVALID, reply_markup={"remove_keyboard": True})

    admin_flow(m, uid, cmd, arg)


def handle_callback(cq):
    data = cq.get("data", "")
    uid = cq["from"]["id"]
    if data.startswith("chk:"):
        code = data[4:]
        if not q("SELECT 1 FROM links WHERE code=?", (code,), one=True):
            return tg("answerCallbackQuery", callback_query_id=cq["id"], text="لینک نامعتبر است", show_alert=True)
        if not is_admin(uid) and not_joined(uid):
            return tg("answerCallbackQuery", callback_query_id=cq["id"],
                      text="هنوز در همه کانال‌ها عضو نشدی ❗", show_alert=True)
        tg("answerCallbackQuery", callback_query_id=cq["id"])
        chat = cq["message"]["chat"]["id"]
        tg("deleteMessage", chat_id=chat, message_id=cq["message"]["message_id"])
        send_content(chat, code)


@app.route("/webhook", methods=["POST"])
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != config.WEBHOOK_SECRET:
        return "forbidden", 403
    upd = request.get_json(silent=True) or {}
    try:
        if "message" in upd:
            handle_message(upd["message"])
        elif "callback_query" in upd:
            handle_callback(upd["callback_query"])
    except Exception as e:
        print("error:", e)
    return "ok"


@app.route("/")
def home():
    return "ok"
