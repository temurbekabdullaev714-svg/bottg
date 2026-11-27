"""
Телеграм-бот: фото → стикер, стикер-паки, видео → GIF, Pinterest → PNG/MP4.
Всё управление кнопками, команд в чате не нужно (кроме первого /start).

Запуск:
    pip install -r requirements.txt
    (ffmpeg ставится сам через imageio-ffmpeg)
    export BOT_TOKEN="токен от @BotFather"
    python bot.py
"""
import asyncio
import hashlib
import html
import json
import logging
import os
import re
import tempfile
import uuid
from pathlib import Path

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, InputSticker, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder
import imageio_ffmpeg
import yt_dlp
from PIL import Image

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
TOKEN = os.getenv("BOT_TOKEN", "")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
PIN_RE = re.compile(r"https?://(?:[\w-]+\.)?(?:pinterest\.[a-z.]+|pin\.it)/[^\s]+", re.I)

router = Router()
locks: dict[int, asyncio.Lock] = {}


class Pack(StatesGroup):
    title = State()
    collecting = State()


# ───────────────────────── Интерфейс ─────────────────────────

MENU_TEXT = (
    "✨ <b>Мастерская стикеров и медиа</b>\n\n"
    "Просто пришли мне что-нибудь — я сам пойму, что делать:\n\n"
    "🖼 <b>Фото</b> → стикер или целый пак\n"
    "🎭 <b>Видео</b> → анимированный стикер, пак или GIF\n"
    "📌 <b>Ссылка Pinterest</b> → PNG или MP4\n"
    "🐦 <b>Ссылка X (Twitter)</b> → видео или GIF\n"
    "▶️ <b>Ссылка YouTube / Shorts</b> → видео или аудио\n\n"
    "Или выбери действие ниже 👇"
)

HINTS = {
    "sticker": "🖼 <b>Фото → стикер</b>\n\nПришли любое фото — получишь готовый стикер 512×512.",
    "vsticker": "🎭 <b>Видео → стикер</b>\n\nПришли видео или GIF — сделаю из него "
                "<b>движущийся стикер</b> (в стикер попадают первые 3 секунды, "
                "это правило Telegram) или целый видео-пак.",
    "gif": "🎞 <b>Видео → GIF</b>\n\nПришли видео (до ~15 сек работает лучше всего) — "
           "верну GIF-анимацию и файл .gif.",
    "x": "🐦 <b>X (Twitter)</b>\n\nПришли ссылку на пост (x.com/…/status/… или twitter.com/…/status/…).\n"
         "Верну видео или GIF из поста. Если в посте их нет или аккаунт закрытый — не получится.",
    "yt": "▶️ <b>YouTube / Shorts</b>\n\nПришли ссылку на ролик (youtube.com/watch…, youtu.be/…, youtube.com/shorts/…).\n"
          "Выберешь: <b>видео MP4</b> (до 720p) или <b>только аудио</b>.\n"
          "Ограничения: ролик до 20 минут, файл до 50 МБ (лимит Telegram для ботов).",
    "pin": "📌 <b>Pinterest</b>\n\nПришли ссылку на пин (pinterest.com/pin/… или pin.it/…).\n"
           "Фото придёт в <b>PNG</b>, видео — в <b>MP4</b>.",
}


def main_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🖼 Фото → стикер", callback_data="hint:sticker"),
          InlineKeyboardButton(text="🎭 Видео → стикер", callback_data="hint:vsticker"))
    b.row(InlineKeyboardButton(text="🎞 Видео → GIF", callback_data="hint:gif"),
          InlineKeyboardButton(text="📦 Стикер-пак", callback_data="pack:new"))
    b.row(InlineKeyboardButton(text="📌 Pinterest", callback_data="hint:pin"),
          InlineKeyboardButton(text="🐦 X (Twitter)", callback_data="hint:x"))
    b.row(InlineKeyboardButton(text="▶️ YouTube / Shorts", callback_data="hint:yt"))
    return b.as_markup()


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")]])


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(MENU_TEXT, reply_markup=main_kb())


@router.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text(MENU_TEXT, reply_markup=main_kb())
    await c.answer()


@router.callback_query(F.data.startswith("hint:"))
async def cb_hint(c: CallbackQuery):
    await c.message.edit_text(HINTS[c.data.split(":")[1]], reply_markup=back_kb())
    await c.answer()


# ───────────────────────── Утилиты ─────────────────────────

def to_sticker_webp(src: Path, dst: Path):
    im = Image.open(src).convert("RGBA")
    k = 512 / max(im.size)
    im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))),
                   Image.LANCZOS)
    im.save(dst, "WEBP", quality=95)


async def run(*cmd: str) -> int:
    p = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    return await p.wait()


async def download_url(session: aiohttp.ClientSession, url: str, dst: Path,
                       referer: str = "https://www.pinterest.com/") -> bool:
    headers = {**UA, "Referer": referer}
    async with session.get(url, headers=headers) as r:
        if r.status != 200:
            logging.warning("Скачивание %s -> HTTP %s", url, r.status)
            return False
        dst.write_bytes(await r.read())
        return True


# ───────────────────────── Стикеры: фото и видео ─────────────────────────

MAX_DL = 20 * 1024 * 1024  # лимит Telegram на скачивание файла ботом


class MixError(Exception):
    pass


def photo_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🖼 Один стикер", callback_data="photo:single"),
          InlineKeyboardButton(text="📦 В новый пак", callback_data="photo:pack"))
    return b.as_markup()


def video_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🎭 Видео-стикер", callback_data="video:single"),
          InlineKeyboardButton(text="📦 В новый пак", callback_data="video:pack"))
    b.row(InlineKeyboardButton(text="🎞 В GIF", callback_data="video:gif"))
    return b.as_markup()


def media_of(msg: Message):
    """Возвращает (kind, file_id, size) для фото или видео/GIF, иначе None."""
    if msg is None:
        return None
    if msg.photo:
        return "photo", msg.photo[-1].file_id, msg.photo[-1].file_size
    v = msg.video or msg.animation
    if v:
        return "video", v.file_id, v.file_size
    return None


async def to_video_sticker(src: Path, dst: Path) -> bool:
    """Видео → WEBM VP9 по правилам Telegram: сторона 512, до 3 сек, без звука, до 256 КБ."""
    for br in (500, 300, 180, 100):
        code = await run(
            FFMPEG, "-y", "-i", str(src), "-t", "3", "-an",
            "-vf", "scale=512:512:force_original_aspect_ratio=decrease,fps=30",
            "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
            "-b:v", f"{br}k", "-maxrate", f"{br}k", "-bufsize", f"{br * 2}k",
            "-auto-alt-ref", "0", "-deadline", "good", "-cpu-used", "5", str(dst))
        if code == 0 and dst.exists() and dst.stat().st_size <= 256 * 1024:
            return True
    return False


async def build_sticker(bot: Bot, kind: str, file_id: str, tmp: Path):
    """Скачивает файл и готовит стикер. Возвращает (путь, формат)."""
    if kind == "photo":
        src, dst = tmp / "in.jpg", tmp / "s.webp"
        await bot.download(file_id, destination=src)
        to_sticker_webp(src, dst)
        return dst, "static"
    src, dst = tmp / "in.mp4", tmp / "s.webm"
    await bot.download(file_id, destination=src)
    if not await to_video_sticker(src, dst):
        raise RuntimeError("не удалось собрать видео-стикер")
    return dst, "video"


async def put_in_pack(bot: Bot, user_id: int, data: dict, kind: str, file_id: str) -> str:
    """Добавляет стикер в пак (создаёт пак при первом). Возвращает формат пака."""
    want = "static" if kind == "photo" else "video"
    if data.get("fmt") and data["fmt"] != want:
        raise MixError()
    with tempfile.TemporaryDirectory() as tmp:
        path, fmt = await build_sticker(bot, kind, file_id, Path(tmp))
        st = InputSticker(sticker=FSInputFile(path), format=fmt, emoji_list=["😀"])
        if data.get("count", 0) == 0:
            await bot.create_new_sticker_set(
                user_id=user_id, name=data["name"], title=data["title"], stickers=[st])
        else:
            await bot.add_sticker_to_set(user_id=user_id, name=data["name"], sticker=st)
    return fmt


@router.message(Pack.collecting, F.photo | F.video | F.animation)
async def pack_add(m: Message, state: FSMContext, bot: Bot):
    kind, file_id, size = media_of(m)
    if kind == "video" and size and size > MAX_DL:
        return await m.answer("😔 Видео больше 20 МБ — Telegram не даёт боту его скачать.",
                              reply_markup=pack_done_kb())
    lock = locks.setdefault(m.from_user.id, asyncio.Lock())
    async with lock:
        data = await state.get_data()
        status = await m.answer("⏳ Добавляю в пак…")
        try:
            fmt = await put_in_pack(bot, m.from_user.id, data, kind, file_id)
            count = data.get("count", 0) + 1
            await state.update_data(count=count, fmt=fmt)
            more = "фото" if fmt == "static" else "видео"
            await status.edit_text(
                f"✅ Стикер №{count} добавлен!\nПришли ещё {more} или нажми «Готово».",
                reply_markup=pack_done_kb())
        except MixError:
            await status.edit_text(
                "⚠️ В одном паке нельзя смешивать фото- и видео-стикеры. "
                "Пришли такой же тип, как первый стикер, или нажми «Готово».",
                reply_markup=pack_done_kb())
        except Exception as e:
            logging.exception(e)
            await status.edit_text("😔 Не получилось добавить. Попробуй другой файл.",
                                   reply_markup=pack_done_kb())


@router.message(F.photo)
async def on_photo(m: Message):
    await m.reply("Что сделать с этим фото?", reply_markup=photo_kb())


@router.callback_query(F.data.in_({"photo:single", "video:single"}))
async def cb_single(c: CallbackQuery, bot: Bot):
    info = media_of(c.message.reply_to_message)
    want = "photo" if c.data == "photo:single" else "video"
    if not info or info[0] != want:
        return await c.answer("Пришли файл ещё раз 🙏", show_alert=True)
    kind, file_id, size = info
    if kind == "video" and size and size > MAX_DL:
        return await c.answer("Видео больше 20 МБ — Telegram не даёт его скачать.",
                              show_alert=True)
    await c.message.edit_text("⏳ Делаю стикер…")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path, _ = await build_sticker(bot, kind, file_id, Path(tmp))
            await c.message.answer_sticker(FSInputFile(path))
        await c.message.edit_text(
            "✅ Готово! Перешли стикер в «Избранное», чтобы сохранить.",
            reply_markup=back_kb())
    except Exception as e:
        logging.exception(e)
        await c.message.edit_text("😔 Не получилось. Попробуй другой файл.",
                                  reply_markup=back_kb())
    await c.answer()


# ───────────────────────── Стикер-паки ─────────────────────────

def pack_done_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="✅ Готово", callback_data="pack:done"))
    b.row(InlineKeyboardButton(text="✖️ Отмена", callback_data="menu"))
    return b.as_markup()


@router.callback_query(F.data == "pack:new")
async def cb_pack_new(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(Pack.title)
    await c.message.edit_text(
        "📦 <b>Новый стикер-пак</b>\n\nНапиши название пака (до 64 символов).",
        reply_markup=back_kb())
    await c.answer()


@router.callback_query(F.data.in_({"photo:pack", "video:pack"}))
async def cb_media_pack(c: CallbackQuery, state: FSMContext):
    info = media_of(c.message.reply_to_message)
    want = "photo" if c.data == "photo:pack" else "video"
    if not info or info[0] != want:
        return await c.answer("Пришли файл ещё раз 🙏", show_alert=True)
    kind, file_id, size = info
    if kind == "video" and size and size > MAX_DL:
        return await c.answer("Видео больше 20 МБ — Telegram не даёт его скачать.",
                              show_alert=True)
    await state.clear()
    await state.update_data(first=file_id, first_kind=kind)
    await state.set_state(Pack.title)
    what = "фото" if kind == "photo" else "видео"
    await c.message.edit_text(
        f"📦 <b>Новый стикер-пак</b>\n\nЭто {what} станет первым стикером.\n"
        "Напиши название пака (до 64 символов).", reply_markup=back_kb())
    await c.answer()


@router.message(Pack.title, F.text)
async def pack_title(m: Message, state: FSMContext, bot: Bot):
    title = m.text.strip()[:64]
    me = await bot.me()
    name = f"p{uuid.uuid4().hex[:10]}_by_{me.username}"
    data = await state.get_data()
    await state.set_state(Pack.collecting)
    await state.update_data(title=title, name=name, count=0, fmt=None)
    if data.get("first"):
        # первый стикер уже есть — создаём пак сразу
        status = await m.answer("⏳ Создаю пак…")
        try:
            cur = {"title": title, "name": name, "count": 0, "fmt": None}
            fmt = await put_in_pack(bot, m.from_user.id, cur, data["first_kind"], data["first"])
            await state.update_data(count=1, fmt=fmt)
            more = "фото" if fmt == "static" else "видео"
            await status.edit_text(
                f"✅ Пак «{html.escape(title)}» создан, стикер №1 добавлен!\n"
                f"Пришли ещё {more} или нажми «Готово».", reply_markup=pack_done_kb())
        except Exception as e:
            logging.exception(e)
            await state.clear()
            await status.edit_text("😔 Не получилось создать пак.", reply_markup=back_kb())
    else:
        await m.answer(f"📦 Пак «{html.escape(title)}»\n\nТеперь присылай <b>фото</b> "
                       "(обычные стикеры) или <b>видео</b> (движущиеся стикеры). "
                       "Тип первого стикера станет типом всего пака.",
                       reply_markup=pack_done_kb())


@router.callback_query(F.data == "pack:done")
async def cb_pack_done(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    if not data.get("count"):
        await c.message.edit_text("Пак пустой — ничего не создано.", reply_markup=back_kb())
        return await c.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить пак себе",
                              url=f"https://t.me/addstickers/{data['name']}")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")]])
    await c.message.edit_text(
        f"🎉 Пак «{html.escape(data['title'])}» готов!\nСтикеров: {data['count']}",
        reply_markup=kb)
    await c.answer()


# ───────────────────────── Видео: выбор действия и GIF ─────────────────────────

@router.message(F.video | F.animation)
async def on_video(m: Message):
    await m.reply("Что сделать с этим видео?", reply_markup=video_kb())


@router.callback_query(F.data == "video:gif")
async def cb_gif(c: CallbackQuery, bot: Bot):
    info = media_of(c.message.reply_to_message)
    if not info or info[0] != "video":
        return await c.answer("Пришли видео ещё раз 🙏", show_alert=True)
    _, file_id, size = info
    if size and size > MAX_DL:
        return await c.answer("Видео больше 20 МБ — Telegram не даёт его скачать.",
                              show_alert=True)
    reply_to = c.message.reply_to_message
    await c.message.edit_text("⏳ Превращаю в GIF…")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.mp4"
            mp4, gif = Path(tmp) / "out.mp4", Path(tmp) / "out.gif"
            await bot.download(file_id, destination=src)
            c1 = await run(FFMPEG, "-y", "-i", str(src), "-t", "15", "-an",
                           "-vf", "scale=480:-2", "-c:v", "libx264",
                           "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4))
            c2 = await run(FFMPEG, "-y", "-i", str(src), "-t", "10", "-vf",
                           "fps=15,scale=480:-1:flags=lanczos,split[a][b];"
                           "[a]palettegen[p];[b][p]paletteuse", str(gif))
            if c1 != 0 or c2 != 0:
                raise RuntimeError("ffmpeg failed")
            await reply_to.answer_animation(FSInputFile(mp4))
            await reply_to.answer_document(FSInputFile(gif), caption="📎 Файл .gif")
        await c.message.edit_text("✅ Готово!", reply_markup=back_kb())
    except Exception as e:
        logging.exception(e)
        await c.message.edit_text("😔 Не получилось. Попробуй другое видео.",
                                  reply_markup=back_kb())
    await c.answer()


# ───────────────────────── Pinterest ─────────────────────────

def find_all(obj, key):
    """Рекурсивно ищет все значения по ключу в JSON."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            yield from find_all(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from find_all(v, key)


def best_video(video_lists) -> str | None:
    items = []
    for vl in video_lists:
        if isinstance(vl, dict):
            items += [v for v in vl.values() if isinstance(v, dict) and v.get("url")]
    mp4 = [v for v in items if v["url"].endswith(".mp4")]
    if mp4:
        return max(mp4, key=lambda v: v.get("width") or 0)["url"]
    hls = [v for v in items if ".m3u8" in v["url"]]
    return hls[0]["url"] if hls else None


def iter_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from iter_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_dicts(v)


def pin_from_page(raw: str, pin_id: str):
    """Ищет данные пина в JSON, встроенном в HTML страницы."""
    for m in re.finditer(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', raw, re.S):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        for d in iter_dicts(data):
            if str(d.get("id")) == pin_id and (
                    "images" in d or "videos" in d or "story_pin_data" in d):
                return d
    return None


async def pinterest_fetch(url: str, tmp: Path):
    """Возвращает (kind, path): kind = 'image' | 'video'."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
        # 1. раскрываем короткую ссылку, достаём id пина
        async with s.get(url, headers=UA, allow_redirects=True) as r:
            final, raw_page = str(r.url), await r.text()
        page = html.unescape(raw_page).replace("\\/", "/")
        mid = (re.search(r"/pin/(?:[^/?]*--)?(\d+)", final)
               or re.search(r"/pin/(?:[^/?]*--)?(\d+)", url))
        pin_id = mid.group(1) if mid else None

        video_url, image_urls = None, []

        # 2. внутренний JSON Pinterest (без ключей)
        if pin_id:
            try:
                q = json.dumps({"options": {"id": pin_id, "field_set_key": "detailed"},
                                "context": {}})
                async with s.get("https://www.pinterest.com/resource/PinResource/get/",
                                 params={"data": q},
                                 headers={**UA, "X-Requested-With": "XMLHttpRequest",
                                          "Accept": "application/json"}) as r:
                    logging.info("Pinterest JSON: HTTP %s", r.status)
                    j = await r.json(content_type=None)
                d = (j.get("resource_response") or {}).get("data") or {}
                video_url = best_video(find_all(d, "video_list"))
                for o in find_all(d, "orig"):
                    if isinstance(o, dict) and o.get("url"):
                        image_urls.append(o["url"])
                        break
            except Exception as e:
                logging.warning("Pinterest JSON не сработал: %r", e)

        # 2.5. данные пина, встроенные в саму страницу
        if pin_id and not video_url and not image_urls:
            pin = pin_from_page(raw_page, pin_id)
            if pin:
                logging.info("Pinterest: пин найден в JSON страницы")
                video_url = best_video(find_all(pin, "video_list"))
                for o in find_all(pin, "orig"):
                    if isinstance(o, dict) and o.get("url"):
                        image_urls.insert(0, o["url"])
                        break
            else:
                logging.warning("Pinterest: в JSON страницы пин %s не найден", pin_id)

        # 3. запасной путь: разбор HTML страницы
        if not video_url:
            vid = re.search(r'https://v\d*\.pinimg\.com/videos/[^"\\\s]+?\.mp4', page)
            hls = re.search(r'https://v\d*\.pinimg\.com/[^"\\\s]+?\.m3u8', page)
            video_url = (vid or hls).group(0) if (vid or hls) else None
        og = (re.search(r'property="og:image"\s+content="([^"]+)"', page)
              or re.search(r'content="([^"]+)"\s+property="og:image"', page)
              or re.search(r'name="og:image"\s+content="([^"]+)"', page))
        if og:
            raw = og.group(1)
            image_urls += [re.sub(r"/\d+x/", "/originals/", raw), raw]
        image_urls += re.findall(r'https://i\.pinimg\.com/originals/[^"\\\s]+', page)[:1]

        # 3.5. официальный oEmbed Pinterest (отдаёт хотя бы превью картинки)
        try:
            async with s.get("https://www.pinterest.com/oembed.json",
                             params={"url": f"https://www.pinterest.com/pin/{pin_id}/"}, headers=UA) as r:
                logging.info("Pinterest oEmbed: HTTP %s", r.status)
                oj = await r.json(content_type=None)
            t = oj.get("thumbnail_url")
            if t:
                image_urls += [re.sub(r"/\d+x/", "/originals/", t), t]
        except Exception as e:
            logging.warning("Pinterest oEmbed не сработал: %r", e)

        logging.info("Pinterest: final=%s page_len=%s id=%s video=%s images=%s",
                     final, len(page), pin_id, bool(video_url), len(image_urls))

        # 4. скачиваем: видео в приоритете
        if video_url:
            out = tmp / "pin.mp4"
            if ".m3u8" in video_url:
                if await run(FFMPEG, "-y", "-i", video_url, "-c", "copy", str(out)) == 0:
                    return "video", out
            elif await download_url(s, video_url, out):
                return "video", out
        for u in image_urls:
            f = tmp / "pin_raw"
            try:
                if await download_url(s, u, f):
                    png = tmp / "pin.png"
                    Image.open(f).convert("RGBA").save(png, "PNG")
                    return "image", png
            except Exception as e:
                logging.warning("Картинка %s не открылась: %r", u, e)
    logging.warning("Pinterest: ничего не найдено для %s (id=%s)", url, pin_id)
    return None, None


@router.message(F.text.regexp(PIN_RE))
async def on_pinterest(m: Message):
    url = PIN_RE.search(m.text).group(0)
    status = await m.reply("⏳ Достаю из Pinterest…")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            kind, path = await pinterest_fetch(url, Path(tmp))
            if kind == "video":
                await m.answer_document(FSInputFile(path, filename="pinterest.mp4"),
                                        caption="🎬 MP4")
            elif kind == "image":
                await m.answer_document(FSInputFile(path, filename="pinterest.png"),
                                        caption="🖼 PNG")
            else:
                return await status.edit_text(
                    "😔 Не нашёл медиа по этой ссылке (пин приватный или Pinterest "
                    "поменял страницу).", reply_markup=back_kb())
        await status.edit_text("✅ Готово!", reply_markup=back_kb())
    except Exception as e:
        logging.exception(e)
        await status.edit_text("😔 Ошибка при скачивании. Попробуй ещё раз.",
                               reply_markup=back_kb())


# ───────────────────────── X (Twitter) ─────────────────────────

X_RE = re.compile(r"https?://(?:www\.|mobile\.)?(?:twitter\.com|x\.com|fxtwitter\.com|vxtwitter\.com)"
                  r"/(\w+)/status(?:es)?/(\d+)", re.I)


async def x_fetch(user: str, tid: str, tmp: Path):
    """Возвращает список (kind, path), kind = 'gif' | 'video'."""
    found = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as s:
        try:
            async with s.get(f"https://api.fxtwitter.com/{user}/status/{tid}", headers=UA) as r:
                logging.info("fxtwitter: HTTP %s", r.status)
                j = await r.json(content_type=None)
            media = ((j.get("tweet") or {}).get("media") or {})
            for it in media.get("all") or []:
                if it.get("type") in ("video", "gif") and it.get("url"):
                    found.append((it["type"], it["url"]))
        except Exception as e:
            logging.warning("fxtwitter не сработал: %r", e)
        if not found:
            try:
                async with s.get(f"https://api.vxtwitter.com/{user}/status/{tid}", headers=UA) as r:
                    logging.info("vxtwitter: HTTP %s", r.status)
                    j = await r.json(content_type=None)
                for it in j.get("media_extended") or []:
                    if it.get("type") in ("video", "gif") and it.get("url"):
                        found.append((it["type"], it["url"]))
            except Exception as e:
                logging.warning("vxtwitter не сработал: %r", e)
        results = []
        for i, (kind, u) in enumerate(found[:4]):
            f = tmp / f"x{i}.mp4"
            if await download_url(s, u, f, referer="https://x.com/"):
                results.append((kind, f))
    return results


@router.message(F.text.regexp(X_RE))
async def on_x(m: Message):
    mm = X_RE.search(m.text)
    status = await m.reply("⏳ Достаю из X…")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            items = await x_fetch(mm.group(1), mm.group(2), Path(tmp))
            if not items:
                return await status.edit_text(
                    "😔 Не нашёл видео или GIF в этом посте (их там нет, аккаунт "
                    "закрыт или пост удалён).", reply_markup=back_kb())
            for kind, path in items:
                if path.stat().st_size > 49 * 1024 * 1024:
                    await m.answer("😔 Видео больше 50 МБ, Telegram не даёт отправить его ботом.")
                    continue
                if kind == "gif":
                    await m.answer_animation(FSInputFile(path))
                else:
                    await m.answer_video(FSInputFile(path), supports_streaming=True)
        await status.edit_text("✅ Готово!", reply_markup=back_kb())
    except Exception as e:
        logging.exception(e)
        await status.edit_text("😔 Ошибка при скачивании. Попробуй ещё раз.",
                               reply_markup=back_kb())


# ───────────────────────── YouTube / Shorts ─────────────────────────

YT_RE = re.compile(r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?\S*|shorts/\S+|live/\S+)"
                   r"|youtu\.be/\S+)", re.I)
MAX_SEND = 49 * 1024 * 1024   # лимит Telegram на отправку файла ботом
MAX_YT_SEC = 20 * 60


class TooBig(Exception):
    pass


class TooLong(Exception):
    pass


def yt_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🎬 Видео MP4", callback_data="yt:video"),
          InlineKeyboardButton(text="🎵 Только аудио", callback_data="yt:audio"))
    return b.as_markup()


def yt_download(url: str, mode: str, tmp: Path):
    """Блокирующая загрузка через yt-dlp (запускается в отдельном потоке)."""
    opts = {
        "outtmpl": str(tmp / "%(id)s.%(ext)s"), "noplaylist": True, "quiet": True,
        "no_warnings": True, "ffmpeg_location": FFMPEG, "socket_timeout": 30, "retries": 3,
    }
    cookies = os.getenv("YT_COOKIES")  # необязательно: путь к cookies.txt
    if cookies and os.path.exists(cookies):
        opts["cookiefile"] = cookies
    if mode == "audio":
        opts["format"] = "bestaudio[ext=m4a]/bestaudio"
    else:
        opts["format"] = ("bv*[height<=720][ext=mp4][filesize<45M]+ba[ext=m4a]/"
                          "b[height<=720][ext=mp4][filesize<45M]/"
                          "bv*[height<=480][ext=mp4]+ba[ext=m4a]/b[height<=480][ext=mp4]/b")
        opts["merge_output_format"] = "mp4"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if (info.get("duration") or 0) > MAX_YT_SEC:
            raise TooLong()
        ydl.process_ie_result(info, download=True)
    files = [f for f in tmp.iterdir() if f.is_file() and f.suffix not in (".part", ".ytdl")]
    if not files:
        raise RuntimeError("файл не скачался")
    return max(files, key=lambda f: f.stat().st_size), info.get("title") or "video"


@router.message(F.text.regexp(YT_RE))
async def on_youtube(m: Message):
    await m.reply("Что скачать?", reply_markup=yt_kb())


@router.callback_query(F.data.in_({"yt:video", "yt:audio"}))
async def cb_youtube(c: CallbackQuery):
    src = c.message.reply_to_message
    mm = YT_RE.search(src.text or "") if src else None
    if not mm:
        return await c.answer("Пришли ссылку ещё раз 🙏", show_alert=True)
    mode = c.data.split(":")[1]
    await c.message.edit_text("⏳ Скачиваю… это может занять до минуты")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path, title = await asyncio.to_thread(yt_download, mm.group(0), mode, Path(tmp))
            if path.stat().st_size > MAX_SEND:
                raise TooBig()
            if mode == "audio":
                await src.answer_audio(FSInputFile(path), title=title[:64])
            else:
                await src.answer_video(FSInputFile(path), caption=title[:200],
                                       supports_streaming=True)
        await c.message.edit_text("✅ Готово!", reply_markup=back_kb())
    except TooLong:
        await c.message.edit_text("😔 Ролик длиннее 20 минут — такие я не скачиваю.",
                                  reply_markup=back_kb())
    except TooBig:
        await c.message.edit_text(
            "😔 Файл больше 50 МБ, Telegram не даёт отправить его ботом. "
            "Попробуй «Только аудио» или ролик покороче.", reply_markup=back_kb())
    except Exception as e:
        logging.exception(e)
        await c.message.edit_text(
            "😔 Не получилось скачать. Ролик может быть закрытым, с возрастным "
            "ограничением, или YouTube блокирует запросы с сервера.",
            reply_markup=back_kb())
    await c.answer()


# ───────────────────────── Всё остальное ─────────────────────────

@router.message()
async def fallback(m: Message):
    await m.answer(MENU_TEXT, reply_markup=main_kb())


async def main():
    logging.basicConfig(level=logging.INFO)
    if not TOKEN:
        raise SystemExit("Задай переменную окружения BOT_TOKEN")
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # На Render есть RENDER_EXTERNAL_URL -> работаем через webhook.
    # Локально (без этой переменной) -> обычный polling.
    base = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("WEBHOOK_URL")
    if not base:
        await bot.delete_webhook(drop_pending_updates=True)
        return await dp.start_polling(bot)

    secret = hashlib.sha256(TOKEN.encode()).hexdigest()[:32]
    path = "/webhook"

    async def on_startup(bot: Bot):
        await bot.set_webhook(base.rstrip("/") + path, secret_token=secret,
                              drop_pending_updates=True)

    dp.startup.register(on_startup)
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="ok"))
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=secret).register(app, path=path)
    setup_application(app, dp, bot=bot)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000"))).start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
