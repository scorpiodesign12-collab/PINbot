"""
PinShare — backend для Vercel + Supabase.

Делает то же самое, что и версия для PythonAnywhere, только вместо
SQLite (которая не подходит для serverless — файловая система там
временная и сбрасывается между запросами) используется Supabase
(Postgres) как база данных.

НАСТРОЙКА:
  1. Используем уже существующий проект Supabase — peredatsha (не
     создаём новый, чтобы не упереться в лимит бесплатного плана).
     В этом проекте, в SQL Editor, выполните запрос из файла
     supabase_schema.sql — он создаст отдельную таблицу
     pinshare_connections, никак не пересекающуюся с тем, что там уже есть.
  2. В настройках проекта peredatsha (Settings → API) скопируйте:
       - Project URL          -> переменная окружения SUPABASE_URL
       - service_role key     -> переменная окружения SUPABASE_SERVICE_KEY
     (именно service_role, а не anon — иначе запись будет блокироваться
     политиками безопасности).
  3. Создайте бота через @BotFather, получите токен.
  4. В Vercel (Project → Settings → Environment Variables) добавьте:
       PINSHARE_BOT_TOKEN, PINSHARE_BOT_USERNAME, PINSHARE_WEBHOOK_SECRET,
       SUPABASE_URL, SUPABASE_SERVICE_KEY
  5. Задеплойте проект на Vercel (импортировав репозиторий с GitHub).
  6. Один раз откройте в браузере:
     https://api.telegram.org/bot<ТОКЕН>/setWebhook?url=https://<ваш-домен>.vercel.app/webhook/<WEBHOOK_SECRET>
"""

import os
import time
import uuid

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
TOKEN_TTL_SECONDS = 15 * 60  # ссылка-приглашение живёт 15 минут

app = Flask(__name__)

_supabase = None


def db():
    """Ленивая инициализация клиента Supabase (важно для serverless — не
    подключаемся, пока функция реально не вызвана)."""
    global _supabase
    if _supabase is None:
        _supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _supabase


def tg_api(method, **params):
    r = requests.post(f"{API_URL}/{method}", json=params, timeout=10)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- API подключения аккаунта

@app.route("/api/connect/start", methods=["POST"])
def connect_start():
    """Сайт вызывает это, чтобы получить одноразовую ссылку на бота."""
    token = uuid.uuid4().hex[:12]
    db().table("pinshare_connections").insert(
        {"token": token, "status": "pending", "created_at": time.time()}
    ).execute()
    bot_link = f"https://t.me/{BOT_USERNAME}?start={token}"
    return jsonify({"token": token, "botLink": bot_link})


@app.route("/api/connect/status")
def connect_status():
    """Сайт опрашивает это, пока ждёт подтверждения в Telegram."""
    token = request.args.get("token", "")
    res = db().table("pinshare_connections").select("*").eq("token", token).limit(1).execute()
    row = res.data[0] if res.data else None

    if not row:
        return jsonify({"status": "not_found"}), 404

    if row["status"] == "pending" and time.time() - row["created_at"] > TOKEN_TTL_SECONDS:
        return jsonify({"status": "expired"})

    data = {"status": row["status"]}
    if row["status"] == "connected":
        data.update(
            {
                "tgId": row["tg_id"],
                "username": row["username"],
                "firstName": row["first_name"],
                "avatarUrl": f"/api/avatar/{row['tg_id']}" if row.get("photo_file_id") else None,
            }
        )
    return jsonify(data)


@app.route("/api/avatar/<int:tg_id>")
def avatar(tg_id):
    """Проксирует аватарку из Telegram, не раскрывая токен бота клиенту."""
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


# ---------------------------------------------------------------- вебхук бота

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

    # пробуем получить аватарку пользователя
    photo_file_id = None
    try:
        photos = tg_api("getUserProfilePhotos", user_id=from_user["id"], limit=1)
        result = photos.get("result", {})
        if result.get("total_count", 0) > 0:
            sizes = result["photos"][0]
            photo_file_id = sizes[-1]["file_id"]  # самый крупный доступный размер
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
