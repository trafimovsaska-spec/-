import logging

from pyrogram import Client
from pyrogram.errors import (
    FloodWait, PhoneNumberInvalid, PhoneNumberBanned, RPCError,
)
from pyrogram.raw import functions, types

log = logging.getLogger("checker")

CODE_TYPE_INFO = {
    "SentCodeTypeApp": ("app", "Код доставлен В ПРИЛОЖЕНИЕ Telegram"),
    "SentCodeTypeSms": ("sms", "Код отправлен по SMS"),
    "SentCodeTypeCall": ("call", "Код придёт звонком"),
    "SentCodeTypeMissedCall": ("missed_call", "Код — пропущенный звонок"),
    "SentCodeTypeFlashCall": ("flash_call", "Flash-call"),
    "SentCodeTypeEmailCode": ("email", "Код на почту"),
    "SentCodeTypeSetUpEmailRequired": ("email_required", "Telegram требует привязать почту"),
}

SMS_LIKE = {"sms", "call", "missed_call", "flash_call"}


async def check_by_code(client: Client, phone: str) -> dict:
    """Анонимная проверка: Telegram сам скажет, куда доставил код."""
    res = {"registered": None, "code_channel": None, "error": None, "note": ""}
    try:
        sent = await client.send_code(phone)
    except PhoneNumberInvalid:
        res.update(registered=False, error="PHONE_NUMBER_INVALID",
                   note="Номер не существует или неверный формат")
        return res
    except PhoneNumberBanned:
        res.update(registered=False, error="PHONE_NUMBER_BANNED",
                   note="Номер заблокирован Telegram")
        return res
    except FloodWait as e:
        res.update(error=f"FLOOD_WAIT_{e.value}", note=f"Лимит Telegram: подожди {e.value} сек")
        return res
    except RPCError as e:
        res.update(error=getattr(e, "MESSAGE", type(e).__name__), note=str(e))
        return res

    type_name = type(sent.type).__name__
    channel, human = CODE_TYPE_INFO.get(type_name, (type_name, type_name))
    res["code_channel"] = channel

    if channel == "app":
        res["registered"] = True
        res["note"] = "Есть активные сессии — аккаунт точно существует"
    else:
        res["note"] = (f"{human}. SMS/звонок приходят и зарегистрированным (без активных сессий), "
                       f"и пустым номерам (код регистрации) — однозначно сказать нельзя")
    return res


async def check_by_contact(client: Client, phone: str) -> dict:
    """Точная проверка через импорт контакта (нужен проверочный аккаунт)."""
    res = {"registered": None, "error": None, "note": "", "username": None}
    try:
        imported = await client.invoke(
            functions.contacts.ImportContacts(
                contacts=[types.InputPhoneContact(
                    client_id=0, phone=phone, first_name="Check", last_name=""
                )]
            )
        )
        users = imported.users or []
        if users:
            u = users[0]
            res["registered"] = True
            res["username"] = getattr(u, "username", None)
            res["note"] = "Номер привязан к аккаунту (найден через контакты)"
            try:
                await client.invoke(functions.contacts.DeleteContacts(id=[u.id]))
            except Exception:
                pass
        else:
            res["registered"] = False
            res["note"] = "Аккаунт не найден (либо владелец скрыл номер в приватности)"
    except FloodWait as e:
        res["error"] = f"FLOOD_WAIT_{e.value}"
        res["note"] = f"Лимит: подожди {e.value} сек"
    except RPCError as e:
        res["error"] = getattr(e, "MESSAGE", type(e).__name__)
        res["note"] = str(e)
    return res


def combine(code_res: dict | None, contact_res: dict | None) -> tuple[str, str]:
    """Итоговый вердикт: (verdict, confidence)."""
    for r in (code_res, contact_res):
        if r and str(r.get("error") or "").startswith("FLOOD"):
            return "🕒 LIMIT", "flood"

    if code_res and code_res["registered"] is True:
        return "✅ ЗАРЕГИСТРИРОВАН", "высокая"
    if contact_res and contact_res["registered"] is True:
        return "✅ ЗАРЕГИСТРИРОВАН", "высокая"

    if code_res and code_res["registered"] is False:
        return "❌ НЕ ЗАРЕГИСТРИРОВАН", "высокая"

    if contact_res and contact_res["registered"] is False:
        return "❌ НЕ ЗАРЕГИСТРИРОВАН", "средняя"

    if code_res and code_res["code_channel"] in SMS_LIKE:
        return "❓ НЕИЗВЕСТНО", "низкая (SMS пришёл бы и на пустой номер)"

    return "❓ НЕИЗВЕСТНО", "низкая"
