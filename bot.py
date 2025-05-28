import os
import re
import uuid
import json
import base64
import logging
import asyncio

from dotenv import load_dotenv
from bs4 import BeautifulSoup
import requests
from telegram import Update, ReplyKeyboardRemove, ReplyKeyboardMarkup, Chat
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ─── ЗАГРУЗКА КОНФИГА ───────────────────────────────
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
VERCEL_TOKEN       = os.getenv("VERCEL_TOKEN")
PROJECTS_FILE      = "projects.json"
if not TELEGRAM_BOT_TOKEN or not VERCEL_TOKEN:
    raise RuntimeError("В .env нужны TELEGRAM_BOT_TOKEN и VERCEL_TOKEN")

# ─── ЛОГИРОВАНИЕ ────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ─── STATE CONSTANTS ────────────────────────────────
LINK1, LINK2, LINK3, ASK_TITLE, ASK_LOGO, EDIT_KEY, EDIT_CHOICE, EDIT_NEW = range(8)

# временное хранилище сессий
user_state: dict[int, dict] = {}

# ─── HELPERS: JSON PROJECTS ────────────────────────
def load_projects() -> dict:
    if os.path.exists(PROJECTS_FILE):
        with open(PROJECTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_projects(d: dict):
    with open(PROJECTS_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

# ─── ВАЛИДАЦИЯ TELEGRAM-ССЫЛКИ ─────────────────────
def normalize_tg_link(text: str) -> str:
    t = text.strip()
    m = re.match(r"^@?([A-Za-z0-9_]{5,32})$", t)
    if m:
        return f"https://t.me/{m.group(1)}"
    m2 = re.match(r"^(https?://)?t\.me/([A-Za-z0-9_]{5,32})$", t, re.IGNORECASE)
    if m2:
        return f"https://t.me/{m2.group(2)}"
    raise ValueError("❌ Допустимы только Telegram-ссылки: @username или https://t.me/username")

# ─── FETCH OPEN GRAPH ──────────────────────────────
def fetch_og_metadata(url: str):
    try:
        r = requests.get(url, timeout=5, headers={"User-Agent":"Mozilla/5.0"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        og_title = soup.find("meta", property="og:title")
        og_image = soup.find("meta", property="og:image")
        title = og_title["content"] if og_title and og_title.get("content") else None
        img_url = og_image["content"] if og_image and og_image.get("content") else None
        if img_url:
            ir = requests.get(img_url, timeout=5, headers={"User-Agent":"Mozilla/5.0"})
            ir.raise_for_status()
            ext = img_url.split("?")[0].rsplit(".",1)[-1]
            logo_name = f"logo.{ext}"
            return title, ir.content, logo_name
        return title, None, None
    except Exception:
        return None

# ─── TELEGRAM CHAT META ────────────────────────────
async def try_fetch_chat_meta(ctx: ContextTypes.DEFAULT_TYPE, url: str):
    username = url.rsplit("/",1)[-1]
    try:
        chat: Chat = await ctx.bot.get_chat(f"@{username}")
        title = chat.title or chat.first_name or chat.username or username
        logo_bytes = None
        logo_name = None
        if chat.photo and chat.photo.small_file_id:
            tg_file = await ctx.bot.get_file(chat.photo.small_file_id)
            logo_bytes = await tg_file.download_as_bytearray()
            ext = tg_file.file_path.rsplit(".",1)[-1]
            logo_name = f"logo.{ext}"
        return title, logo_bytes, logo_name
    except Exception:
        return None

# ─── DEPLOY TO VERCEL ──────────────────────────────
async def deploy_to_vercel(html: str, css: str,
                           logo_entries: list[tuple[bytes,str]],
                           project: str=None) -> (str,str):
    """
    logo_entries: список кортежей (bytes, filename), например:
      [(b'...', 'logo1.png'), (b'...', 'logo2.jpg'), (b'...', 'logo3.png')]
    """
    if project is None:
        project = f"multilink-{uuid.uuid4().hex[:6]}"
    files = [
        {
          "file": "index.html",
          "data": base64.b64encode(html.encode()).decode(),
          "encoding": "base64"
        },
        {
          "file": "style.css",
          "data": base64.b64encode(css.encode()).decode(),
          "encoding": "base64"
        },
    ]
    # добавляем все логотипы
    for logo_bytes, logo_name in logo_entries:
        files.append({
          "file": logo_name,
          "data": base64.b64encode(logo_bytes).decode(),
          "encoding": "base64"
        })

    payload = {
        "name": project,
        "files": files,
        "projectSettings": {
            "framework": None,
            "rootDirectory": None,
            "outputDirectory": None,
            "installCommand": None,
            "buildCommand": None,
            "devCommand": None
        },
        "target": "production"
    }
    headers = {
      "Authorization": f"Bearer {VERCEL_TOKEN}",
      "Content-Type": "application/json"
    }
    r = requests.post("https://api.vercel.com/v13/deployments", json=payload, headers=headers)
    if r.status_code >= 400:
        logger.error("Vercel response %s: %s", r.status_code, r.text)
    r.raise_for_status()
    return f"https://{project}.vercel.app", project

# ─── CREATE FLOW ──────────────────────────────────
async def create_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user_state[update.effective_user.id] = {}
    await update.message.reply_text("1️⃣ Пришлите ссылку на основного бота:")
    return LINK1

async def create_link1(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    try:
        link1 = normalize_tg_link(update.message.text)
    except ValueError as e:
        await update.message.reply_text(str(e))
        return LINK1

    s = user_state[uid]
    s["link1"] = link1

    meta = fetch_og_metadata(link1) or await try_fetch_chat_meta(ctx, link1)
    if meta:
        title, logo_bytes, logo_name = meta
        if title:
            s["title"] = title
        if logo_bytes:
            s["logo1_bytes"] = logo_bytes
            s["logo1_name"]  = logo_name

    if "title" not in s:
        await update.message.reply_text("🏷 Пришлите название сайта:")
        return ASK_TITLE
    if "logo1_bytes" not in s:
        await update.message.reply_text("📸 Пришлите логотип (фото):")
        return ASK_LOGO

    await update.message.reply_text("2️⃣ Пришлите ссылку на резервного бота:")
    return LINK2

async def create_ask_title(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    user_state[uid]["title"] = update.message.text.strip()
    await update.message.reply_text("📸 Пришлите логотип (фото):")
    return ASK_LOGO

async def create_ask_logo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    photo = update.message.photo[-1]
    tg_f = await photo.get_file()
    logo_bytes = await tg_f.download_as_bytearray()
    ext = tg_f.file_path.rsplit(".",1)[-1]
    user_state[uid]["logo1_bytes"] = logo_bytes
    user_state[uid]["logo1_name"]  = f"logo.{ext}"
    await update.message.reply_text("2️⃣ Пришлите ссылку на резервного бота:")
    return LINK2

async def create_link2(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    try:
        link2 = normalize_tg_link(update.message.text)
    except ValueError as e:
        await update.message.reply_text(str(e))
        return LINK2

    s = user_state[uid]
    s["link2"] = link2

    # OG / Telegram
    meta = fetch_og_metadata(link2) or await try_fetch_chat_meta(ctx, link2)
    if meta and meta[1]:
        _, logo_bytes2, logo_name2 = meta
        s["logo2_bytes"] = logo_bytes2
        s["logo2_name"]  = logo_name2
    else:
        # если нет — ставим logo1
        s["logo2_bytes"] = s["logo1_bytes"]
        s["logo2_name"]  = s["logo1_name"]

    await update.message.reply_text("3️⃣ Пришлите ссылку на канал:")
    return LINK3

async def create_link3(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    try:
        link3 = normalize_tg_link(update.message.text)
    except ValueError as e:
        await update.message.reply_text(str(e))
        return LINK3

    s = user_state[uid]
    s["link3"] = link3

    meta = fetch_og_metadata(link3) or await try_fetch_chat_meta(ctx, link3)
    if meta and meta[1]:
        _, logo_bytes3, logo_name3 = meta
        s["logo3_bytes"] = logo_bytes3
        s["logo3_name"]  = logo_name3
    else:
        s["logo3_bytes"] = s["logo1_bytes"]
        s["logo3_name"]  = s["logo1_name"]

    return await _finalize_creation(update, ctx)

async def _finalize_creation(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    s = user_state.pop(uid)

    with open("template/index.html","r",encoding="utf-8") as f: tpl_html = f.read()
    with open("template/style.css","r",encoding="utf-8") as f:  tpl_css  = f.read()

    html = (
        tpl_html
        .replace("%%TITLE%%",  s["title"])
        .replace("%%LOGO%%",   s["logo1_name"])
        .replace("%%LINK1%%",  s["link1"])
        .replace("%%LOGO1%%",  s["logo1_name"])
        .replace("%%LINK2%%",  s["link2"])
        .replace("%%LOGO2%%",  s["logo2_name"])
        .replace("%%LINK3%%",  s["link3"])
        .replace("%%LOGO3%%",  s["logo3_name"])
    )

    # готовим три логотипа
    logos = [
        (s["logo1_bytes"], s["logo1_name"]),
        (s["logo2_bytes"], s["logo2_name"]),
        (s["logo3_bytes"], s["logo3_name"]),
    ]

    url, project = await deploy_to_vercel(tpl_html.replace("%%TITLE%%",s["title"]), tpl_css, logos, project=None)

    projs = load_projects()
    key = uuid.uuid4().hex[:16]
    projs[key] = {
        "project":    project,
        "title":      s["title"],
        "link1":      s["link1"],
        "link2":      s["link2"],
        "link3":      s["link3"],
        "logo1_name": s["logo1_name"],
        "logo2_name": s["logo2_name"],
        "logo3_name": s["logo3_name"],
        "logo1_data": base64.b64encode(s["logo1_bytes"]).decode(),
        "logo2_data": base64.b64encode(s["logo2_bytes"]).decode(),
        "logo3_data": base64.b64encode(s["logo3_bytes"]).decode(),
    }
    save_projects(projs)

    await update.message.reply_text(
        f"🎉 Готово!\nСайт: {url}\n\n"
        f"🔑 Ключ для редактирования: `{key}`\n"
        "Используйте /edit, чтобы обновить ссылки.",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

# ─── EDIT FLOW ─────────────────────────────────────
async def edit_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("🔑 Введите ваш ключ для редактирования:")
    return EDIT_KEY

async def edit_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    key = update.message.text.strip()
    projs = load_projects()
    if key not in projs:
        await update.message.reply_text("❌ Ключ не найден.")
        return ConversationHandler.END
    ctx.user_data["edit_key"] = key
    kb = ReplyKeyboardMarkup(
        [["Основной бот","Резервный бот"],["Канал","Отмена"]],
        one_time_keyboard=True, resize_keyboard=True
    )
    await update.message.reply_text("Что меняем?", reply_markup=kb)
    return EDIT_CHOICE

async def edit_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text.strip().lower()
    if choice == "отмена":
        await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    ctx.user_data["edit_choice"] = choice
    prompts = {
        "основной бот":  "Новый URL основного бота:",
        "резервный бот": "Новый URL резервного бота:",
        "канал":         "Новый URL канала:",
    }
    await update.message.reply_text(prompts[choice], reply_markup=ReplyKeyboardRemove())
    return EDIT_NEW

async def edit_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    try:
        new = normalize_tg_link(raw)
    except ValueError as e:
        await update.message.reply_text(str(e))
        return EDIT_NEW

    key    = ctx.user_data["edit_key"]
    choice = ctx.user_data["edit_choice"]
    projs  = load_projects()
    entry  = projs[key]
    field  = {"основной бот":"link1","резервный бот":"link2","канал":"link3"}[choice]
    entry[field] = new
    save_projects(projs)

    with open("template/index.html","r",encoding="utf-8") as f: tpl_html = f.read()
    with open("template/style.css","r",encoding="utf-8") as f:  tpl_css  = f.read()

    html = (
        tpl_html
        .replace("%%TITLE%%",  entry["title"])
        .replace("%%LOGO%%",   entry["logo1_name"])
        .replace("%%LINK1%%",  entry["link1"])
        .replace("%%LOGO1%%",  entry["logo1_name"])
        .replace("%%LINK2%%",  entry["link2"])
        .replace("%%LOGO2%%",  entry["logo2_name"])
        .replace("%%LINK3%%",  entry["link3"])
        .replace("%%LOGO3%%",  entry["logo3_name"])
    )

    logos = [
        (base64.b64decode(entry["logo1_data"]), entry["logo1_name"]),
        (base64.b64decode(entry["logo2_data"]), entry["logo2_name"]),
        (base64.b64decode(entry["logo3_data"]), entry["logo3_name"]),
    ]

    url, _ = await deploy_to_vercel(html, tpl_css, logos, project=entry["project"])
    await update.message.reply_text(f"✅ Обновлено! Ваш сайт: {url}")
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user_state.pop(update.effective_user.id, None)
    await update.message.reply_text("Отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ─── MAIN ───────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start",  create_start),
            CommandHandler("create", create_start),
            CommandHandler("edit",   edit_start),
        ],
        states={
            LINK1:       [MessageHandler(filters.TEXT & ~filters.COMMAND, create_link1)],
            ASK_TITLE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, create_ask_title)],
            ASK_LOGO:    [MessageHandler(filters.PHOTO,                  create_ask_logo)],
            LINK2:       [MessageHandler(filters.TEXT & ~filters.COMMAND, create_link2)],
            LINK3:       [MessageHandler(filters.TEXT & ~filters.COMMAND, create_link3)],
            EDIT_KEY:    [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_key)],
            EDIT_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_choice)],
            EDIT_NEW:    [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_new)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)
    app.run_polling()

if __name__ == "__main__":
    main()
