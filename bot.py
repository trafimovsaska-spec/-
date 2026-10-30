import asyncio
import html
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field

from aiohttp import web
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, TelegramObject, BotCommand
from aiogram.utils.keyboard import InlineKeyboardBuilder

from pyrogram import Client
from pyrogram.errors import (
    SessionPasswordNeeded, PhoneCodeExpired, PhoneCodeInvalid,
    PhoneNumberInvalid, FloodWait,
)
from pyrogram.raw import functions

from config import settings
from database import (
    init_db, add_checker, get_checkers, remove_checker, save_check, get_history,
)
from checker import check_by_code, check_by_contact, combine

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logging.getLogger("pyrogram").setLevel(logging.WARNING)
log = logging.getLogger("bot")


class Check(StatesGroup):
    phone = State()
    method = State()
    batch = State()


class AuthCheck(StatesGroup):
    phone = State()
    code = State()
    password = State()


@dataclass
class PendingLogin:
    client: Client
    phone: str
    phone_code_hash: str
    created_at: float = field(default_factory=time.time)
    hint: str = ""


pending: dict[tuple[int, str], PendingLogin] = {}

bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())


class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        allowed = settings.allowed_ids
        user = getattr(event, "from_user", None)
        if allowed and (user is None or user.id not in allowed):
            if isinstance(event, Message):
                await event.answer("⛔ Бот закрыт для посторонних.")
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔ Бот закрыт для посторонних.", show_alert=True)
            return None
        return await handler(event, data)


dp.message.outer_middleware(AccessMiddleware())
dp.callback_query.outer_middleware(AccessMiddleware())


def esc(t: str) -> str:
    return html.escape(t or "")


def valid_phone(t: str) -> bool:
    return bool(re.match(r"^\+[1-9]\d{7,14}$", t.strip()))


async def drop_pending(owner_id: int, phone: str):
    item = pending.pop((owner_id, phone), None)
    if item:
        try:
            await item.client.disconnect()
        except Exception:
            pass


async def clean_pending_loop():
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for key, item in list(pending.items()):
            if now - item.created_at > 600:
                await drop_pending(*key)


# ---------------- Веб-сервер для Render ----------------
async def health_check(request):
    return web.Response(text="OK", status=200)


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(f"Веб-сервер на порту {port}")
    return runner


# ---------------- Меню ----------------
def menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔎 Проверить номер", callback_data="menu:one")
    kb.button(text="📋 Массовая проверка", callback_data="menu:batch")
    kb.button(text="🧑💼 Проверочные аккаунты", callback_data="menu:checkers")
    kb.button(text="📜 История", callback_data="menu:history")
    kb.adjust(2)
    return kb.as_markup()


@dp.message(Command("start", "help"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🔎 <b>Проверка номеров Telegram</b>\n\n"
        "Определяю, зарегистрирован ли номер:\n"
        "• <b>по коду</b> — куда Telegram доставил код авторизации\n"
        "• <b>по контакту</b> — через свой проверочный аккаунт (точнее)\n\n"
        "⚠️ Проверяй только свои номера или с согласия владельца.\n"
        "Частые запросы = FloodWait/бан от Telegram.",
        reply_markup=menu_kb(),
    )


@dp.callback_query(F.data.startswith("menu:"))
async def cb_menu(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split(":")[1]

    if action == "one":
        await state.set_state(Check.phone)
        await callback.message.answer("Введи номер для проверки:\n<code>+79991234567</code>")

    elif action == "batch":
        await state.set_state(Check.batch)
        await callback.message.answer(
            f"Введи список номеров (по одному в строке, до {settings.BATCH_LIMIT}):\n"
            "<code>+79991111111\n+79992222222</code>"
        )

    elif action == "checkers":
        checkers = await get_checkers(callback.from_user.id)
        kb = InlineKeyboardBuilder()
        for phone, _ in checkers:
            kb.button(text=f"🗑 {phone}", callback_data=f"delchk:{phone}")
        kb.button(text="➕ Добавить аккаунт", callback_data="addchk")
        kb.adjust(1)
        text = "🧑‍💼 <b>Проверочные аккаунты</b> (для метода «по контакту»):\n\n"
        text += "\n".join(f"• {p}" for p, _ in checkers) if checkers else "Пока нет."
        await callback.message.answer(text, reply_markup=kb.as_markup())

    elif action == "history":
        rows = await get_history(callback.from_user.id)
        if not rows:
            await callback.message.answer("📭 Историй пока нет.")
        else:
            text = "📜 <b>Последние проверки</b>\n\n"
            for phone, verdict, details, created in rows:
                text += f"{phone} → {verdict}\n   {esc(details[:80])}\n\n"
            await callback.message.answer(text[:3900])

    await callback.answer()


# ---------------- Одиночная проверка ----------------
@dp.message(Check.phone)
async def check_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    if not valid_phone(phone):
        await message.answer("❌ Формат: <code>+79991234567</code>")
        return

    await state.update_data(target=phone)

    checkers = await get_checkers(message.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="📨 По коду (без аккаунта)", callback_data="method:code")
    if checkers:
        kb.button(text="👥 По контакту (точнее)", callback_data="method:contact")
        kb.button(text="🔄 Оба метода", callback_data="method:both")
    kb.adjust(1)

    await state.set_state(Check.method)
    await message.answer(f"Номер: <code>{esc(phone)}</code>\nВыбери метод:", reply_markup=kb.as_markup())


@dp.callback_query(Check.method, F.data.startswith("method:"))
async def cb_method(callback: CallbackQuery, state: FSMContext):
    method = callback.data.split(":")[1]
    data = await state.get_data()
    phone = data["target"]
    owner = callback.from_user.id

    await state.clear()
    msg = await callback.message.answer(f"⏳ Проверяю {esc(phone)}…")

    code_res, contact_res = None, None

    # Метод по коду: анонимный клиент
    if method in ("code", "both"):
        anon = Client(
            name=f"anon_{owner}_{int(time.time())}",
            api_id=settings.API_ID,
            api_hash=settings.API_HASH,
            in_memory=True,
            no_updates=True,
        )
        try:
            await anon.connect()
            code_res = await check_by_code(anon, phone)
        finally:
            try:
                await anon.disconnect()
            except Exception:
                pass

    # Метод по контакту: проверочный аккаунт
    if method in ("contact", "both"):
        checkers = await get_checkers(owner)
        if not checkers:
            await msg.edit_text("❌ Нет проверочного аккаунта. Добавь через меню.")
            await callback.answer()
            return
        session = checkers[0][1]
        acc = Client(
            name=f"chk_{owner}_{int(time.time())}",
            api_id=settings.API_ID,
            api_hash=settings.API_HASH,
            session_string=session,
            in_memory=True,
            no_updates=True,
        )
        try:
            await acc.start()
            contact_res = await check_by_contact(acc, phone)
        except Exception as e:
            contact_res = {"registered": None, "error": type(e).__name__, "note": str(e), "username": None}
        finally:
            try:
                await acc.stop()
            except Exception:
                pass

    verdict, confidence = combine(code_res, contact_res)

    lines = [f"🔎 <code>{esc(phone)}</code>\n", f"<b>Вердикт:</b> {verdict}", f"<b>Уверенность:</b> {confidence}\n"]
    if code_res:
        ch = code_res.get("code_channel") or code_res.get("error") or "—"
        lines.append(f"📨 Код: {ch} — {esc(code_res['note'])}")
    if contact_res:
        lines.append(f"👥 Контакт: {esc(contact_res['note'])}")
        if contact_res.get("username"):
            lines.append(f" Username: @{contact_res['username']}")

    await save_check(owner, phone, verdict, " | ".join(lines))
    await msg.edit_text("\n".join(lines))
    await callback.answer()


# ---------------- Массовая проверка ----------------
@dp.message(Check.batch)
async def check_batch(message: Message, state: FSMContext):
    phones = [l.strip() for l in message.text.splitlines() if valid_phone(l.strip())]
    phones = list(dict.fromkeys(phones))[: settings.BATCH_LIMIT]

    if not phones:
        await message.answer("❌ Не нашёл валидных номеров. По одному в строке: <code>+7999...</code>")
        return

    await state.clear()
    checkers = await get_checkers(message.from_user.id)
    use_contact = bool(checkers)

    msg = await message.answer(f"⏳ Проверяю {len(phones)} номеров… Паузы {settings.CHECK_DELAY_MIN}-{settings.CHECK_DELAY_MAX}с.")

    anon = Client(
        name=f"anonb_{message.from_user.id}_{int(time.time())}",
        api_id=settings.API_ID,
        api_hash=settings.API_HASH,
        in_memory=True,
        no_updates=True,
    )
    acc = None
    if use_contact:
        acc = Client(
            name=f"chkb_{message.from_user.id}_{int(time.time())}",
            api_id=settings.API_ID,
            api_hash=settings.API_HASH,
            session_string=checkers[0][1],
            in_memory=True,
            no_updates=True,
        )

    results = []
    try:
        await anon.connect()
        if acc:
            await acc.start()

        for i, phone in enumerate(phones, 1):
            code_res = await check_by_code(anon, phone)
            contact_res = await check_by_contact(acc, phone) if acc else None
            verdict, conf = combine(code_res, contact_res)
            results.append(f"{phone} → {verdict}")
            await save_check(message.from_user.id, phone, verdict, conf)

            if verdict == "🕒 LIMIT":
                results.append("⛔ Дальше нельзя: FloodWait. Повтори позже.")
                break

            await msg.edit_text(
                f"⏳ Прогресс: {i}/{len(phones)}\n\n" + "\n".join(results[-10:])
            )
            if i < len(phones):
                await asyncio.sleep(random.uniform(settings.CHECK_DELAY_MIN, settings.CHECK_DELAY_MAX))
    finally:
        for c in (anon, acc):
            try:
                await c.stop() if c and c.is_connected else None
            except Exception:
                pass

    await msg.edit_text(f"🏁 <b>Готово</b> ({len(phones)})\n\n" + "\n".join(results))


# ---------------- Проверочные аккаунты ----------------
@dp.callback_query(F.data == "addchk")
async def cb_addchk(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AuthCheck.phone)
    await callback.message.answer(
        "📱 Введи номер проверочного аккаунта:\n<code>+79991234567</code>\n\n"
        "Код придёт в Telegram этого аккаунта."
    )
    await callback.answer()


@dp.message(AuthCheck.phone)
async def auth_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    if not valid_phone(phone):
        await message.answer("❌ Формат: <code>+79991234567</code>")
        return

    owner = message.from_user.id
    await drop_pending(owner, phone)

    client = Client(
        name=f"login_{owner}_{int(time.time())}",
        api_id=settings.API_ID,
        api_hash=settings.API_HASH,
        in_memory=True,
        no_updates=True,
    )
    try:
        await client.connect()
        sent = await client.send_code(phone)
    except PhoneNumberInvalid:
        await client.disconnect()
        await message.answer("❌ Номер не существует.")
        await state.clear()
        return
    except FloodWait as e:
        await client.disconnect()
        await message.answer(f"🕒 Подожди {e.value} сек и повтори.")
        await state.clear()
        return
    except Exception as e:
        await client.disconnect()
        await message.answer(f"❌ Ошибка: <code>{esc(str(e))}</code>")
        await state.clear()
        return

    pending[(owner, phone)] = PendingLogin(client, phone, sent.phone_code_hash)
    await state.set_state(AuthCheck.code)
    await state.update_data(phone=phone)

    type_name = type(sent.type).__name__
    where = ("в приложении Telegram этого аккаунта (чат «Telegram»)"
             if type_name == "SentCodeTypeApp" else "по SMS/звонку")
    await message.answer(f"📩 Код пришёл {where}. Пришли его цифрами:")


@dp.message(AuthCheck.code)
async def auth_code(message: Message, state: FSMContext):
    data = await state.get_data()
    phone = data.get("phone")
    owner = message.from_user.id
    item = pending.get((owner, phone))
    if not item:
        await message.answer("❌ Сессия истекла. Начни заново.")
        await state.clear()
        return

    try:
        await item.client.sign_in(phone, item.phone_code_hash, message.text.strip())
    except SessionPasswordNeeded:
        try:
            pw = await item.client.invoke(functions.account.GetPassword())
            item.hint = getattr(pw, "hint", "") or ""
        except Exception:
            pass
        await state.set_state(AuthCheck.password)
        await message.answer(f"🔐 Нужен облачный пароль 2FA. Подсказка: {esc(item.hint) or 'нет'}")
        return
    except (PhoneCodeInvalid, PhoneCodeExpired):
        await drop_pending(owner, phone)
        await state.clear()
        await message.answer("❌ Неверный/устаревший код. Заново.")
        return
    except Exception as e:
        await drop_pending(owner, phone)
        await state.clear()
        await message.answer(f"❌ Ошибка: <code>{esc(str(e))}</code>")
        return

    await finish_login(message, state, owner, phone, item.client)


@dp.message(AuthCheck.password)
async def auth_password(message: Message, state: FSMContext):
    data = await state.get_data()
    phone = data.get("phone")
    owner = message.from_user.id
    item = pending.get((owner, phone))
    if not item:
        await state.clear()
        return
    try:
        await item.client.check_password(message.text)
    except Exception:
        await message.answer("❌ Неверный пароль. Ещё раз:")
        return
    await finish_login(message, state, owner, phone, item.client)


async def finish_login(message, state, owner, phone, client):
    try:
        session = await client.export_session_string()
        ok = await add_checker(owner, phone, session)
        await message.answer(
            f"✅ Проверочный аккаунт {phone} подключён!" if ok
            else "❌ Не удалось сохранить."
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: <code>{esc(str(e))}</code>")
    finally:
        await drop_pending(owner, phone)
        await state.clear()


@dp.callback_query(F.data.startswith("delchk:"))
async def cb_delchk(callback: CallbackQuery):
    phone = callback.data.split(":", 1)[1]
    ok = await remove_checker(callback.from_user.id, phone)
    await callback.message.answer(f"✅ Удалён {phone}" if ok else "❌ Не найдено")
    await callback.answer()


# ---------------- Запуск ----------------
async def main():
    await init_db()
    log.info("БД готова. Старт…")

    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
    ])

    web_runner = await start_web_server()
    task = asyncio.create_task(clean_pending_loop())
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        task.cancel()
        for key in list(pending):
            await drop_pending(*key)
        await bot.session.close()
        await web_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
