"""
PinShare — backend для Vercel + Supabase.

v3: добавлены реальные бизнес-эндпоинты (задания, заявки, баланс,
рейтинг, история), которых раньше не было — раньше вся эта логика жила
только в localStorage браузера, поэтому данные не были видны с других
устройств/аккаунтов и в код был зашит список "фейковых" пользователей
для демонстрации. Теперь всё это уходит в Supabase.

НАСТРОЙКА (см. также supabase_schema_v3.sql):
  1. Выполните supabase_schema_v3.sql в Supabase -> SQL Editor (проект
     peredatsha). Он создаёт таблицы pinshare_users, pinshare_tasks,
     pinshare_submissions, pinshare_transactions, ничего не удаляя.
  2. Создайте публичный bucket "pinshare-proofs" в Supabase -> Storage.
  3. Переменные окружения на Vercel (без изменений):
       PINSHARE_BOT_TOKEN, PINSHARE_BOT_USERNAME, PINSHARE_WEBHOOK_SECRET,
       SUPABASE_URL, SUPABASE_SERVICE_KEY

АВТОРИЗАЦИЯ:
  Токен, который выдаётся в /api/connect/start и подтверждается через
  Telegram-бота, теперь используется как постоянный сессионный токен.
  Клиент передаёт его в заголовке:  Authorization: Bearer <token>
  Всем эндпоинтам ниже (кроме /api/connect/*) он обязателен.
"""

import base64
import os
import time
import uuid
from datetime import datetime, timezone

import requests
from flask import Flask, Response, abort, jsonify, request
from supabase import create_client

# ====================== НАСТРОЙКИ — БЕРУТСЯ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ======================
BOT_TOKEN = os.environ.get("PINSHARE_BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("PINSHARE_BOT_USERNAME", "")  # без @
WEBHOOK_SECRET = os.environ.get("PINSHARE_WEBHOOK_SECRET", "")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
# ===========================================================================================

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
TOKEN_TTL_SECONDS = 15 * 60  # ссылка-приглашение живёт 15 минут, ПОКА не подтверждена

STARTING_BALANCE = 15
PROOFS_BUCKET = "pinshare-proofs"

app = Flask(__name__)

_supabase = None


def db():
    global _supabase
    if _supabase is None:
        _supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _supabase


def tg_api(method, **params):
    r = requests.post(f"{API_URL}/{method}", json=params, timeout=10)
    r.raise_for_status()
    return r.json()


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


@app.errorhandler(ApiError)
def handle_api_error(err):
    return jsonify({"error": err.message}), err.status


# ---------------------------------------------------------------- авторизация

def current_tg_id():
    """Достаёт tg_id из заголовка Authorization: Bearer <token>.
    Токен — это тот же token, что выдаётся /api/connect/start и
    подтверждается ботом; после подтверждения он не истекает и
    работает как постоянная сессия для этого устройства."""
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else ""
    if not token:
        raise ApiError("Не авторизован", 401)

    res = db().table("pinshare_connections").select("*").eq("token", token).limit(1).execute()
    row = res.data[0] if res.data else None
    if not row or row["status"] != "connected" or not row.get("tg_id"):
        raise ApiError("Сессия недействительна, войдите заново", 401)
    return row


def ensure_user(conn_row):
    """Гарантирует, что для этого tg_id есть строка в pinshare_users, и
    подтягивает свежие username/first_name/photo из pinshare_connections."""
    tg_id = conn_row["tg_id"]
    res = db().table("pinshare_users").select("*").eq("tg_id", tg_id).limit(1).execute()
    user = res.data[0] if res.data else None

    patch = {
        "username": conn_row.get("username"),
        "first_name": conn_row.get("first_name"),
        "photo_file_id": conn_row.get("photo_file_id"),
    }

    if not user:
        patch["tg_id"] = tg_id
        patch["balance"] = STARTING_BALANCE
        user = db().table("pinshare_users").insert(patch).execute().data[0]
    else:
        changed = any(user.get(k) != v for k, v in patch.items())
        if changed:
            db().table("pinshare_users").update(patch).eq("tg_id", tg_id).execute()
            user.update(patch)

    return user


def user_public(user):
    return {
        "telegram_id": user["tg_id"],
        "username": user.get("username"),
        "first_name": user.get("first_name") or "Гость",
        "avatar_url": f"/api/avatar/{user['tg_id']}" if user.get("photo_file_id") else None,
        "balance": user.get("balance", 0),
        "total_earned": user.get("total_earned", 0),
        "tasks_completed": user.get("tasks_completed", 0),
        "tasks_created": user.get("tasks_created", 0),
    }


def log_transaction(tg_id, amount, type_, comment=None, related_task_id=None):
    db().table("pinshare_transactions").insert(
        {
            "tg_id": tg_id,
            "amount": amount,
            "type": type_,
            "comment": comment,
            "related_task_id": related_task_id,
        }
    ).execute()


# ---------------------------------------------------------------- API подключения аккаунта (без изменений)

@app.route("/api/connect/start", methods=["POST"])
def connect_start():
    token = uuid.uuid4().hex[:12]
    db().table("pinshare_connections").insert(
        {"token": token, "status": "pending", "created_at": time.time()}
    ).execute()
    bot_link = f"https://t.me/{BOT_USERNAME}?start={token}"
    return jsonify({"token": token, "botLink": bot_link})


@app.route("/api/connect/status")
def connect_status():
    token = request.args.get("token", "")
    res = db().table("pinshare_connections").select("*").eq("token", token).limit(1).execute()
    row = res.data[0] if res.data else None

    if not row:
        return jsonify({"status": "not_found"}), 404

    if row["status"] == "pending" and time.time() - row["created_at"] > TOKEN_TTL_SECONDS:
        return jsonify({"status": "expired"})

    data = {"status": row["status"]}
    if row["status"] == "connected":
        ensure_user(row)
        data.update(
            {
                "token": token,
                "tgId": row["tg_id"],
                "username": row["username"],
                "firstName": row["first_name"],
                "avatarUrl": f"/api/avatar/{row['tg_id']}" if row.get("photo_file_id") else None,
            }
        )
    return jsonify(data)


@app.route("/api/avatar/<int:tg_id>")
def avatar(tg_id):
    res = (
        db()
        .table("pinshare_connections")
        .select("photo_file_id")
        .eq("tg_id", tg_id)
        .not_.is_("photo_file_id", "null")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not res.data:
        abort(404)

    file_info = tg_api("getFile", file_id=res.data[0]["photo_file_id"])
    file_path = file_info["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    resp = requests.get(file_url, timeout=10)
    return Response(resp.content, mimetype="image/jpeg")


# ---------------------------------------------------------------- вебхук бота (без изменений)

@app.route(f"/webhook/{WEBHOOK_SECRET}", methods=["POST"])
def telegram_webhook():
    update = request.get_json(force=True, silent=True) or {}
    message = update.get("message") or update.get("edited_message")
    if not message:
        return jsonify({"ok": True})

    text = message.get("text", "")
    chat_id = message["chat"]["id"]
    from_user = message.get("from", {})

    if not text.startswith("/start"):
        return jsonify({"ok": True})

    parts = text.split(maxsplit=1)
    token = parts[1].strip() if len(parts) > 1 else ""

    if not token:
        tg_api(
            "sendMessage",
            chat_id=chat_id,
            text="Привет! Откройте PinShare на сайте и нажмите «Подключить Telegram» — "
            "ссылка приведёт сюда автоматически.",
        )
        return jsonify({"ok": True})

    res = db().table("pinshare_connections").select("*").eq("token", token).limit(1).execute()
    row = res.data[0] if res.data else None

    if not row:
        tg_api(
            "sendMessage",
            chat_id=chat_id,
            text="Ссылка недействительна или устарела. Откройте страницу подключения на сайте заново.",
        )
        return jsonify({"ok": True})

    photo_file_id = None
    try:
        photos = tg_api("getUserProfilePhotos", user_id=from_user["id"], limit=1)
        result = photos.get("result", {})
        if result.get("total_count", 0) > 0:
            sizes = result["photos"][0]
            photo_file_id = sizes[-1]["file_id"]
    except Exception:
        pass

    db().table("pinshare_connections").update(
        {
            "status": "connected",
            "tg_id": from_user.get("id"),
            "username": from_user.get("username"),
            "first_name": from_user.get("first_name"),
            "photo_file_id": photo_file_id,
        }
    ).eq("token", token).execute()

    display_name = from_user.get("first_name") or from_user.get("username") or "друг"
    tg_api(
        "sendMessage",
        chat_id=chat_id,
        text=f"✅ Вы подключили аккаунт, {display_name}!\n"
        "Вернитесь на сайт PinShare — там уже видно ваш профиль.",
    )
    return jsonify({"ok": True})


# ---------------------------------------------------------------- профиль

@app.route("/api/me")
def api_me():
    conn = current_tg_id()
    user = ensure_user(conn)
    return jsonify(user_public(user))


@app.route("/api/account", methods=["DELETE"])
def api_account_delete():
    """Полностью удаляет пользователя и все его данные из базы:
    его задания, заявки на них (в том числе чужие заявки на его
    задания — иначе останутся заявки без задания), транзакции,
    все его сессии/подключения и сам профиль."""
    conn = current_tg_id()
    tg_id = conn["tg_id"]

    my_tasks = db().table("pinshare_tasks").select("id").eq("creator_tg_id", tg_id).execute().data or []
    my_task_ids = [t["id"] for t in my_tasks]
    if my_task_ids:
        db().table("pinshare_submissions").delete().in_("task_id", my_task_ids).execute()

    db().table("pinshare_submissions").delete().eq("user_tg_id", tg_id).execute()
    db().table("pinshare_tasks").delete().eq("creator_tg_id", tg_id).execute()
    db().table("pinshare_transactions").delete().eq("tg_id", tg_id).execute()
    db().table("pinshare_connections").delete().eq("tg_id", tg_id).execute()
    db().table("pinshare_users").delete().eq("tg_id", tg_id).execute()

    return jsonify({"ok": True})


# ---------------------------------------------------------------- задания

def task_public(task, viewer_tg_id, creator=None, my_submission=None):
    slots_left = max(0, (task.get("needed") or 0) - (task.get("completed_count") or 0))
    return {
        "id": task["id"],
        "title": task["title"],
        "description": task.get("description") or "",
        "target_url": task.get("pin_url"),
        "category_code": task.get("type"),
        "reward": task.get("reward"),
        "slots_total": task.get("needed"),
        "slots_left": slots_left,
        "status": task.get("status"),
        "created_at": task.get("created_at"),
        "is_own": task.get("creator_tg_id") == viewer_tg_id,
        "owner_username": (creator or {}).get("username"),
        "owner_avatar": f"/api/avatar/{task['creator_tg_id']}" if (creator or {}).get("photo_file_id") else None,
        "my_submission_status": my_submission["status"] if my_submission else None,
    }


@app.route("/api/tasks")
def api_tasks_list():
    conn = current_tg_id()
    tg_id = conn["tg_id"]
    category = request.args.get("category") or None

    q = db().table("pinshare_tasks").select("*").eq("status", "active").order("created_at", desc=True)
    if category:
        q = q.eq("type", category)
    tasks = q.execute().data or []

    my_subs_res = db().table("pinshare_submissions").select("task_id,status").eq("user_tg_id", tg_id).execute()
    my_subs = {s["task_id"]: s for s in (my_subs_res.data or [])}

    creator_ids = list({t["creator_tg_id"] for t in tasks})
    creators = {}
    if creator_ids:
        cr = db().table("pinshare_users").select("tg_id,username,photo_file_id").in_("tg_id", creator_ids).execute()
        creators = {c["tg_id"]: c for c in (cr.data or [])}

    result = []
    for t in tasks:
        slots_left = max(0, (t.get("needed") or 0) - (t.get("completed_count") or 0))
        if slots_left <= 0:
            continue
        if t["creator_tg_id"] != tg_id and t["id"] not in my_subs:
            result.append(task_public(t, tg_id, creators.get(t["creator_tg_id"])))
        elif t["creator_tg_id"] == tg_id:
            result.append(task_public(t, tg_id, creators.get(t["creator_tg_id"])))
        else:
            result.append(task_public(t, tg_id, creators.get(t["creator_tg_id"]), my_subs.get(t["id"])))

    return jsonify(result)


@app.route("/api/tasks", methods=["POST"])
def api_tasks_create():
    conn = current_tg_id()
    tg_id = conn["tg_id"]
    user = ensure_user(conn)
    body = request.get_json(force=True, silent=True) or {}

    title = (body.get("title") or "").strip()
    pin_url = (body.get("target_url") or "").strip()
    category = (body.get("category_code") or "other").strip()
    description = (body.get("description") or "").strip()
    reward = int(body.get("reward") or 0)
    needed = int(body.get("slots_total") or 0)

    if not title or not pin_url or reward <= 0 or needed <= 0:
        raise ApiError("Заполните все поля задания", 400)

    cost = reward * needed
    if cost > user.get("balance", 0):
        raise ApiError(f"Недостаточно монет. Нужно {cost}, доступно {user.get('balance', 0)}", 402)

    task = (
        db()
        .table("pinshare_tasks")
        .insert(
            {
                "creator_tg_id": tg_id,
                "type": category,
                "title": title,
                "pin_url": pin_url,
                "description": description,
                "reward": reward,
                "needed": needed,
                "completed_count": 0,
                "status": "active",
            }
        )
        .execute()
        .data[0]
    )

    new_balance = user["balance"] - cost
    db().table("pinshare_users").update(
        {"balance": new_balance, "tasks_created": user.get("tasks_created", 0) + 1}
    ).eq("tg_id", tg_id).execute()
    log_transaction(tg_id, -cost, "task_create_cost", f"Создание задания «{title}»", task["id"])

    return jsonify(task_public(task, tg_id))


@app.route("/api/tasks/<task_id>/cancel", methods=["POST"])
def api_tasks_cancel(task_id):
    conn = current_tg_id()
    tg_id = conn["tg_id"]

    res = db().table("pinshare_tasks").select("*").eq("id", task_id).limit(1).execute()
    task = res.data[0] if res.data else None
    if not task:
        raise ApiError("Задание не найдено", 404)
    if task["creator_tg_id"] != tg_id:
        raise ApiError("Это не ваше задание", 403)

    slots_left = max(0, (task.get("needed") or 0) - (task.get("completed_count") or 0))
    refund = slots_left * task["reward"]

    db().table("pinshare_tasks").update({"status": "cancelled"}).eq("id", task_id).execute()

    if refund > 0:
        user_res = db().table("pinshare_users").select("balance").eq("tg_id", tg_id).limit(1).execute()
        balance = (user_res.data[0]["balance"] if user_res.data else 0) + refund
        db().table("pinshare_users").update({"balance": balance}).eq("tg_id", tg_id).execute()
        log_transaction(tg_id, refund, "task_refund", f"Возврат за отменённое задание «{task['title']}»", task_id)

    return jsonify({"ok": True})


@app.route("/api/tasks/created")
def api_tasks_created():
    conn = current_tg_id()
    tg_id = conn["tg_id"]

    tasks = db().table("pinshare_tasks").select("*").eq("creator_tg_id", tg_id).order("created_at", desc=True).execute().data or []
    task_ids = [t["id"] for t in tasks]
    pending_by_task = {}
    if task_ids:
        subs = db().table("pinshare_submissions").select("task_id").eq("status", "pending").in_("task_id", task_ids).execute().data or []
        for s in subs:
            pending_by_task[s["task_id"]] = pending_by_task.get(s["task_id"], 0) + 1

    result = []
    for t in tasks:
        item = task_public(t, tg_id)
        item["pending_submissions"] = pending_by_task.get(t["id"], 0)
        result.append(item)
    return jsonify(result)


# ---------------------------------------------------------------- заявки (submissions)

def upload_proof_image(data_url, tg_id):
    """data_url — data:image/jpeg;base64,... строка с фронтенда."""
    if not data_url or "," not in data_url:
        raise ApiError("Прикрепите скриншот выполнения", 400)
    header, b64data = data_url.split(",", 1)
    try:
        raw = base64.b64decode(b64data)
    except Exception:
        raise ApiError("Не удалось обработать изображение", 400)

    ext = "png" if "png" in header else "jpg"
    path = f"{tg_id}/{uuid.uuid4().hex}.{ext}"
    content_type = "image/png" if ext == "png" else "image/jpeg"

    db().storage.from_(PROOFS_BUCKET).upload(
        path, raw, {"content-type": content_type}
    )
    return db().storage.from_(PROOFS_BUCKET).get_public_url(path)


@app.route("/api/submissions", methods=["POST"])
def api_submissions_create():
    conn = current_tg_id()
    tg_id = conn["tg_id"]
    body = request.get_json(force=True, silent=True) or {}

    task_id = body.get("task_id")
    res = db().table("pinshare_tasks").select("*").eq("id", task_id).limit(1).execute()
    task = res.data[0] if res.data else None
    if not task:
        raise ApiError("Задание не найдено", 404)
    if task["creator_tg_id"] == tg_id:
        raise ApiError("Нельзя выполнить собственное задание", 400)
    slots_left = max(0, (task.get("needed") or 0) - (task.get("completed_count") or 0))
    if task["status"] != "active" or slots_left <= 0:
        raise ApiError("Задание больше не доступно", 400)

    existing = db().table("pinshare_submissions").select("id").eq("task_id", task_id).eq("user_tg_id", tg_id).execute()
    if existing.data:
        raise ApiError("Вы уже отправляли выполнение этого задания", 409)

    screenshot_url = upload_proof_image(body.get("proof_image"), tg_id)

    submission = (
        db()
        .table("pinshare_submissions")
        .insert(
            {
                "task_id": task_id,
                "user_tg_id": tg_id,
                "screenshot_url": screenshot_url,
                "comment": (body.get("proof_comment") or "").strip(),
                "status": "pending",
            }
        )
        .execute()
        .data[0]
    )
    return jsonify(submission)


@app.route("/api/submissions/mine")
def api_submissions_mine():
    conn = current_tg_id()
    tg_id = conn["tg_id"]
    subs = db().table("pinshare_submissions").select("*").eq("user_tg_id", tg_id).order("created_at", desc=True).execute().data or []

    task_ids = list({s["task_id"] for s in subs})
    tasks = {}
    if task_ids:
        tr = db().table("pinshare_tasks").select("id,title,reward").in_("id", task_ids).execute()
        tasks = {t["id"]: t for t in (tr.data or [])}

    result = []
    for s in subs:
        t = tasks.get(s["task_id"], {})
        result.append(
            {
                "id": s["id"],
                "status": s["status"],
                "submitted_at": s["created_at"],
                "task_title": t.get("title", "—"),
                "task_reward": t.get("reward", 0),
            }
        )
    return jsonify(result)


@app.route("/api/submissions/incoming")
def api_submissions_incoming():
    conn = current_tg_id()
    tg_id = conn["tg_id"]

    my_tasks = db().table("pinshare_tasks").select("id,title,reward").eq("creator_tg_id", tg_id).execute().data or []
    task_ids = [t["id"] for t in my_tasks]
    tasks_by_id = {t["id"]: t for t in my_tasks}
    if not task_ids:
        return jsonify([])

    subs = db().table("pinshare_submissions").select("*").in_("task_id", task_ids).order("created_at", desc=True).execute().data or []

    performer_ids = list({s["user_tg_id"] for s in subs})
    performers = {}
    if performer_ids:
        pr = db().table("pinshare_users").select("tg_id,username,photo_file_id").in_("tg_id", performer_ids).execute()
        performers = {p["tg_id"]: p for p in (pr.data or [])}

    result = []
    for s in subs:
        t = tasks_by_id.get(s["task_id"], {})
        performer = performers.get(s["user_tg_id"], {})
        result.append(
            {
                "id": s["id"],
                "task_id": s["task_id"],
                "status": s["status"],
                "submitted_at": s["created_at"],
                "proof_image": s.get("screenshot_url"),
                "proof_comment": s.get("comment"),
                "task_title": t.get("title", "—"),
                "task_reward": t.get("reward", 0),
                "performer_username": performer.get("username"),
                "performer_avatar": f"/api/avatar/{s['user_tg_id']}" if performer.get("photo_file_id") else None,
            }
        )
    return jsonify(result)


def _review_submission(submission_id, tg_id, approve):
    res = db().table("pinshare_submissions").select("*").eq("id", submission_id).limit(1).execute()
    sub = res.data[0] if res.data else None
    if not sub:
        raise ApiError("Заявка не найдена", 404)
    if sub["status"] != "pending":
        raise ApiError("Заявка уже обработана", 400)

    task_res = db().table("pinshare_tasks").select("*").eq("id", sub["task_id"]).limit(1).execute()
    task = task_res.data[0] if task_res.data else None
    if not task or task["creator_tg_id"] != tg_id:
        raise ApiError("Это не ваше задание", 403)

    new_status = "approved" if approve else "rejected"
    db().table("pinshare_submissions").update(
        {"status": new_status, "reviewed_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", submission_id).execute()

    if approve:
        new_completed = (task.get("completed_count") or 0) + 1
        patch = {"completed_count": new_completed}
        if new_completed >= task["needed"]:
            patch["status"] = "completed"
        db().table("pinshare_tasks").update(patch).eq("id", task["id"]).execute()

        performer_id = sub["user_tg_id"]
        pu = db().table("pinshare_users").select("balance,total_earned,tasks_completed").eq("tg_id", performer_id).limit(1).execute()
        pu_row = pu.data[0] if pu.data else {"balance": 0, "total_earned": 0, "tasks_completed": 0}
        db().table("pinshare_users").update(
            {
                "balance": pu_row["balance"] + task["reward"],
                "total_earned": pu_row["total_earned"] + task["reward"],
                "tasks_completed": pu_row["tasks_completed"] + 1,
            }
        ).eq("tg_id", performer_id).execute()
        log_transaction(performer_id, task["reward"], "task_reward", f"Награда за выполнение «{task['title']}»", task["id"])

    return {"ok": True}


@app.route("/api/submissions/<submission_id>/approve", methods=["POST"])
def api_submissions_approve(submission_id):
    conn = current_tg_id()
    return jsonify(_review_submission(submission_id, conn["tg_id"], True))


@app.route("/api/submissions/<submission_id>/reject", methods=["POST"])
def api_submissions_reject(submission_id):
    conn = current_tg_id()
    return jsonify(_review_submission(submission_id, conn["tg_id"], False))


# ---------------------------------------------------------------- баланс (кликер и пополнение)

@app.route("/api/balance/add", methods=["POST"])
def api_balance_add():
    """Универсальная безопасная точка для начисления монет (используется
    кликером и пополнением баланса) — всегда пишет транзакцию, чтобы
    баланс был согласован между всеми устройствами пользователя."""
    conn = current_tg_id()
    tg_id = conn["tg_id"]
    body = request.get_json(force=True, silent=True) or {}
    amount = int(body.get("amount") or 0)
    type_ = body.get("type") or "adjustment"
    comment = body.get("comment")

    if amount == 0:
        raise ApiError("Некорректная сумма", 400)

    user = ensure_user(conn)
    new_balance = user.get("balance", 0) + amount
    if new_balance < 0:
        raise ApiError("Недостаточно монет", 402)

    patch = {"balance": new_balance}
    if amount > 0 and type_ == "clicker":
        patch["total_earned"] = user.get("total_earned", 0) + amount

    db().table("pinshare_users").update(patch).eq("tg_id", tg_id).execute()
    log_transaction(tg_id, amount, type_, comment)

    return jsonify({"balance": new_balance})


# ---------------------------------------------------------------- история и рейтинг

@app.route("/api/history")
def api_history():
    conn = current_tg_id()
    tg_id = conn["tg_id"]
    txs = db().table("pinshare_transactions").select("*").eq("tg_id", tg_id).order("created_at", desc=True).limit(100).execute().data or []
    return jsonify(txs)


@app.route("/api/rating")
def api_rating():
    conn = current_tg_id()
    tg_id = conn["tg_id"]
    top = db().table("pinshare_users").select("tg_id,username,first_name,photo_file_id,total_earned,tasks_completed").order("total_earned", desc=True).limit(50).execute().data or []

    result = []
    for i, u in enumerate(top):
        result.append(
            {
                "id": u["tg_id"],
                "rank": i + 1,
                "first_name": u.get("first_name"),
                "username": u.get("username"),
                "avatar_url": f"/api/avatar/{u['tg_id']}" if u.get("photo_file_id") else None,
                "total_earned": u.get("total_earned", 0),
                "tasks_completed": u.get("tasks_completed", 0),
                "is_me": u["tg_id"] == tg_id,
            }
        )
    return jsonify(result)
