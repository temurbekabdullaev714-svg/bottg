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
import html
import json
import logging
import os
import re
import tempfile
import uuid
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, InputSticker, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder
import imageio_ffmpeg
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
    "🎞 <b>Видео</b> → GIF\n"
    "📌 <b>Ссылка Pinterest</b> → PNG или MP4\n\n"
    "Или выбери действие ниже 👇"
)

HINTS = {
    "sticker": "🖼 <b>Фото → стикер</b>\n\nПришли любое фото — получишь готовый стикер 512×512.",
    "gif": "🎞 <b>Видео → GIF</b>\n\nПришли видео (до ~15 сек работает лучше всего) — "
           "верну GIF-анимацию и файл .gif.",
    "pin": "📌 <b>Pinterest</b>\n\nПришли ссылку на пин (pinterest.com/pin/… или pin.it/…).\n"
           "Фото придёт в <b>PNG</b>, видео — в <b>MP4</b>.",
}


def main_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🖼 Фото → стикер", callback_data="hint:sticker"),
          InlineKeyboardButton(text="📦 Стикер-пак", callback_data="pack:new"))
    b.row(InlineKeyboardButton(text="🎞 Видео → GIF", callback_data="hint:gif"),
          InlineKeyboardButton(text="📌 Pinterest", callback_data="hint:pin"))
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


async def download_url(session: aiohttp.ClientSession, url: str, dst: Path) -> bool:
    async with session.get(url, headers=UA) as r:
        if r.status != 200:
            return False
        dst.write_bytes(await r.read())
        return True


# ───────────────────────── Фото → стикер ─────────────────────────

def photo_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="🖼 Один стикер", callback_data="photo:single"),
          InlineKeyboardButton(text="📦 В новый пак", callback_data="photo:pack"))
    return b.as_markup()


@router.message(Pack.collecting, F.photo)
async def pack_add_photo(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    lock = locks.setdefault(m.from_user.id, asyncio.Lock())
    async with lock:
        data = await state.get_data()
        status = await m.answer("⏳ Добавляю в пак…")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                src, dst = Path(tmp) / "in.jpg", Path(tmp) / "s.webp"
                await bot.download(m.photo[-1].file_id, destination=src)
                to_sticker_webp(src, dst)
                st = InputSticker(sticker=FSInputFile(dst), format="static",
                                  emoji_list=["😀"])
                if data.get("count", 0) == 0:
                    await bot.create_new_sticker_set(
                        user_id=m.from_user.id, name=data["name"],
                        title=data["title"], stickers=[st])
                else:
                    await bot.add_sticker_to_set(
                        user_id=m.from_user.id, name=data["name"], sticker=st)
            count = data.get("count", 0) + 1
            await state.update_data(count=count)
            await status.edit_text(
                f"✅ Стикер №{count} добавлен!\nПришли ещё фото или нажми «Готово».",
                reply_markup=pack_done_kb())
        except Exception as e:
            logging.exception(e)
            await status.edit_text("😔 Не получилось добавить. Попробуй другое фото.",
                                   reply_markup=pack_done_kb())


@router.message(F.photo)
async def on_photo(m: Message):
    await m.reply("Что сделать с этим фото?", reply_markup=photo_kb())


@router.callback_query(F.data == "photo:single")
async def cb_single(c: CallbackQuery, bot: Bot):
    src_msg = c.message.reply_to_message
    if not src_msg or not src_msg.photo:
        return await c.answer("Пришли фото ещё раз 🙏", show_alert=True)
    await c.message.edit_text("⏳ Делаю стикер…")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "in.jpg", Path(tmp) / "s.webp"
            await bot.download(src_msg.photo[-1].file_id, destination=src)
            to_sticker_webp(src, dst)
            await c.message.answer_sticker(FSInputFile(dst))
        await c.message.edit_text("✅ Готово! Перешли стикер в «Избранное», чтобы сохранить.",
                                  reply_markup=back_kb())
    except Exception as e:
        logging.exception(e)
        await c.message.edit_text("😔 Не получилось. Попробуй другое фото.",
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


@router.callback_query(F.data == "photo:pack")
async def cb_photo_pack(c: CallbackQuery, state: FSMContext):
    src_msg = c.message.reply_to_message
    if not src_msg or not src_msg.photo:
        return await c.answer("Пришли фото ещё раз 🙏", show_alert=True)
    await state.clear()
    await state.update_data(first=src_msg.photo[-1].file_id)
    await state.set_state(Pack.title)
    await c.message.edit_text(
        "📦 <b>Новый стикер-пак</b>\n\nЭто фото станет первым стикером.\n"
        "Напиши название пака (до 64 символов).", reply_markup=back_kb())
    await c.answer()


@router.message(Pack.title, F.text)
async def pack_title(m: Message, state: FSMContext, bot: Bot):
    title = m.text.strip()[:64]
    me = await bot.me()
    name = f"p{uuid.uuid4().hex[:10]}_by_{me.username}"
    data = await state.get_data()
    await state.set_state(Pack.collecting)
    await state.update_data(title=title, name=name, count=0)
    if data.get("first"):
        # первый стикер уже есть — создаём пак сразу
        status = await m.answer("⏳ Создаю пак…")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                src, dst = Path(tmp) / "in.jpg", Path(tmp) / "s.webp"
                await bot.download(data["first"], destination=src)
                to_sticker_webp(src, dst)
                await bot.create_new_sticker_set(
                    user_id=m.from_user.id, name=name, title=title,
                    stickers=[InputSticker(sticker=FSInputFile(dst), format="static",
                                           emoji_list=["😀"])])
            await state.update_data(count=1)
            await status.edit_text(
                f"✅ Пак «{html.escape(title)}» создан, стикер №1 добавлен!\n"
                "Пришли ещё фото или нажми «Готово».", reply_markup=pack_done_kb())
        except Exception as e:
            logging.exception(e)
            await state.clear()
            await status.edit_text("😔 Не получилось создать пак.", reply_markup=back_kb())
    else:
        await m.answer(f"📦 Пак «{html.escape(title)}»\n\nТеперь присылай фото — "
                       "каждое станет стикером.", reply_markup=pack_done_kb())


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


# ───────────────────────── Видео → GIF ─────────────────────────

@router.message(F.video | F.animation)
async def on_video(m: Message, bot: Bot):
    media = m.video or m.animation
    if media.file_size and media.file_size > 20 * 1024 * 1024:
        return await m.reply("😔 Видео больше 20 МБ — Telegram не даёт боту его скачать.")
    status = await m.reply("⏳ Превращаю в GIF…")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.mp4"
            mp4, gif = Path(tmp) / "out.mp4", Path(tmp) / "out.gif"
            await bot.download(media.file_id, destination=src)
            c1 = await run(FFMPEG, "-y", "-i", str(src), "-t", "15", "-an",
                           "-vf", "scale=480:-2", "-c:v", "libx264",
                           "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4))
            c2 = await run(FFMPEG, "-y", "-i", str(src), "-t", "10", "-vf",
                           "fps=15,scale=480:-1:flags=lanczos,split[a][b];"
                           "[a]palettegen[p];[b][p]paletteuse", str(gif))
            if c1 != 0 or c2 != 0:
                raise RuntimeError("ffmpeg failed")
            await m.answer_animation(FSInputFile(mp4))
            await m.answer_document(FSInputFile(gif), caption="📎 Файл .gif")
        await status.edit_text("✅ Готово!", reply_markup=back_kb())
    except Exception as e:
        logging.exception(e)
        await status.edit_text("😔 Не получилось. Попробуй другое видео.",
                               reply_markup=back_kb())


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


async def pinterest_fetch(url: str, tmp: Path):
    """Возвращает (kind, path): kind = 'image' | 'video'."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
        # 1. раскрываем короткую ссылку, достаём id пина
        async with s.get(url, headers=UA, allow_redirects=True) as r:
            final, page = str(r.url), await r.text()
        page = html.unescape(page).replace("\\/", "/")
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
                    j = await r.json(content_type=None)
                d = (j.get("resource_response") or {}).get("data") or {}
                video_url = best_video(find_all(d, "video_list"))
                for o in find_all(d, "orig"):
                    if isinstance(o, dict) and o.get("url"):
                        image_urls.append(o["url"])
                        break
            except Exception as e:
                logging.warning("Pinterest JSON не сработал: %r", e)

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
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
