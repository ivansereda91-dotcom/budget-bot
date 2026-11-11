"""Семейный бот для расходов и бюджета (aiogram 3)."""
import asyncio
import csv
import html
import io
import logging
import os
import re

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BufferedInputFile, CallbackQuery, KeyboardButton, Message,
                           ReplyKeyboardMarkup, TelegramObject)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db
import receipt
import reports

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

TOKEN = os.environ["BOT_TOKEN"]
ALLOWED = {int(x) for x in os.getenv("ALLOWED_IDS", "").replace(" ", "").split(",") if x}
NOTIFY_PARTNER = os.getenv("NOTIFY_PARTNER", "0") == "1"

B_EXP, B_INC, B_REP = "➖ Расход", "➕ Доход", "📊 Отчёт"
B_BUD, B_HIST, B_CATS = "💰 Бюджет", "🧾 История", "🗂 Категории"

MENU_TEXTS = {B_EXP, B_INC, B_REP, B_BUD, B_HIST, B_CATS}
NOT_MENU = ~F.text.in_(MENU_TEXTS)  # кнопки меню всегда работают, даже посреди ввода

MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=B_EXP), KeyboardButton(text=B_INC)],
              [KeyboardButton(text=B_REP), KeyboardButton(text=B_BUD)],
              [KeyboardButton(text=B_HIST), KeyboardButton(text=B_CATS)]],
    resize_keyboard=True,
    input_field_placeholder="12,50 кофе  ·  +2000 зарплата",
)

router = Router()


class Tx(StatesGroup):
    amount = State()
    category = State()


class Lim(StatesGroup):
    amount = State()


class Cat(StatesGroup):
    name = State()


# ---------- доступ ----------

class AccessMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user is None:
            return
        if user.id not in ALLOWED:
            if isinstance(event, Message):
                await event.answer(
                    f"Это частный бот. Твой Telegram ID: <code>{user.id}</code>\n"
                    "Чтобы получить доступ, его нужно добавить в ALLOWED_IDS."
                )
            elif isinstance(event, CallbackQuery):
                await event.answer("Нет доступа", show_alert=True)
            return
        db.upsert_user(user.id, user.first_name or str(user.id))
        return await handler(event, data)


# ---------- общие команды ----------

@router.message(CommandStart())
async def start(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(
        "Привет! Я веду ваш общий бюджет в евро.\n\n"
        "Самый быстрый способ — просто напиши сумму:\n"
        "• <code>12,50 кофе</code> — расход\n"
        "• <code>+2000 зарплата</code> — доход\n"
        "Потом выбери категорию кнопкой.\n\n"
        "/export — выгрузить всё в CSV · /cancel — отменить ввод",
        reply_markup=MENU,
    )


@router.message(Command("cancel"))
@router.message(F.text.lower() == "отмена")
async def cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Отменено.", reply_markup=MENU)


@router.message(Command("id"))
async def my_id(msg: Message):
    await msg.answer(f"Твой ID: <code>{msg.from_user.id}</code>")


# ---------- добавление операций ----------

def cat_keyboard(kind: str):
    kb = InlineKeyboardBuilder()
    for c in db.categories(kind):
        kb.button(text=f"{c['emoji']} {c['name']}", callback_data=f"cat:{c['id']}")
    kb.button(text="✖️ Отмена", callback_data="cat:cancel")
    kb.adjust(2)
    return kb.as_markup()


async def ask_category(msg: Message, state: FSMContext, kind: str, amount: int, comment: str):
    await state.set_state(Tx.category)
    await state.update_data(kind=kind, amount=amount, comment=comment)
    sign = "➖" if kind == "expense" else "➕"
    note = f" · {html.escape(comment)}" if comment else ""
    await msg.answer(f"{sign} <b>{db.fmt(amount)}</b>{note}\nКатегория?", reply_markup=cat_keyboard(kind))


@router.message(F.text == B_EXP)
@router.message(F.text == B_INC)
async def start_tx(msg: Message, state: FSMContext):
    kind = "expense" if msg.text == B_EXP else "income"
    await state.clear()
    await state.set_state(Tx.amount)
    await state.update_data(kind=kind)
    word = "расхода" if kind == "expense" else "дохода"
    await msg.answer(f"Сумма {word}? Можно с комментарием, например <code>12,50 кофе</code>")


@router.message(Tx.amount, F.text, NOT_MENU)
async def got_amount(msg: Message, state: FSMContext):
    parsed = db.parse_amount(msg.text.lstrip("+-"))
    if not parsed:
        await msg.answer("Не понял сумму. Пример: <code>12,50</code> или <code>40 бензин</code>")
        return
    data = await state.get_data()
    amount, comment = parsed
    await ask_category(msg, state, data.get("kind", "expense"), amount, comment or data.get("comment", ""))


@router.callback_query(F.data.startswith("cat:"))
async def got_category(cb: CallbackQuery, state: FSMContext, bot: Bot):
    value = cb.data.split(":", 1)[1]
    data = await state.get_data()
    if value == "cancel":
        await state.clear()
        await cb.message.edit_text("Отменено.")
        return await cb.answer()
    if "amount" not in data:
        await cb.message.edit_text("Запись устарела — введи сумму ещё раз.")
        return await cb.answer()
    await save_tx(cb, bot, state, data, db.category(int(value)))


async def save_tx(cb: CallbackQuery, bot: Bot, state: FSMContext, data: dict, cat):
    tx_id = db.add_tx(data["kind"], data["amount"], cat["id"], cb.from_user.id,
                      data.get("comment", ""), when=data.get("when"))
    await state.clear()

    sign = "➖" if data["kind"] == "expense" else "➕"
    note = f" · {html.escape(data['comment'])}" if data.get("comment") else ""
    day = f" · {data['when'][8:10]}.{data['when'][5:7]}" if data.get("when") else ""
    text = f"✅ {sign} <b>{db.fmt(data['amount'])}</b> — {cat['emoji']} {cat['name']}{note}{day}"
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Отменить", callback_data=f"undo:{tx_id}")
    await cb.message.edit_text(text, reply_markup=kb.as_markup())
    await cb.answer("Записано")

    recipients = set(db.users()) & ALLOWED
    if NOTIFY_PARTNER:
        for uid in recipients - {cb.from_user.id}:
            await safe_send(bot, uid, f"{html.escape(cb.from_user.first_name or '')}: {text[2:]}")
    if data["kind"] == "expense":
        month = data["when"][:7] if data.get("when") else None
        for alert in db.limit_alerts(cat["id"], data["amount"], month):
            for uid in recipients:
                await safe_send(bot, uid, alert)


async def safe_send(bot: Bot, uid: int, text: str):
    try:
        await bot.send_message(uid, text)
    except Exception as e:  # пользователь мог ещё не нажать /start
        logging.warning("Не удалось отправить %s: %s", uid, e)


@router.callback_query(F.data.startswith("undo:"))
async def undo(cb: CallbackQuery):
    tx_id = int(cb.data.split(":")[1])
    tx = db.get_tx(tx_id)
    if tx and db.delete_tx(tx_id):
        await cb.message.edit_text(f"🗑 Удалено: {db.fmt(tx['amount'])} — {tx['emoji']} {tx['cat_name']}")
    else:
        await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer()


# ---------- чеки по фото ----------

def receipt_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Сохранить", callback_data="rcpt:save")
    kb.button(text="🗂 Другая категория", callback_data="rcpt:cat")
    kb.button(text="✏️ Исправить сумму", callback_data="rcpt:amount")
    kb.button(text="✖️ Отмена", callback_data="cat:cancel")
    kb.adjust(1, 2, 1)
    return kb.as_markup()


@router.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def receipt_photo(msg: Message, state: FSMContext, bot: Bot):
    if not receipt.enabled():
        return await msg.answer("Чтение чеков не настроено: нужен ключ Claude API на сервере.")
    await state.clear()
    wait = await msg.answer("🧾 Читаю чек…")
    if msg.photo:
        file_id, mime = msg.photo[-1].file_id, "image/jpeg"
    else:
        file_id, mime = msg.document.file_id, msg.document.mime_type
    buf = io.BytesIO()
    await bot.download(file_id, destination=buf)
    try:
        r = await receipt.parse(buf.getvalue(), mime, db.categories("expense"))
    except receipt.NotAReceipt as e:
        return await wait.edit_text(f"🤔 {html.escape(str(e))}\nМожно ввести вручную: <code>12,50 кофе</code>")
    except Exception:  # noqa: BLE001
        logging.exception("Чек не прочитан")
        return await wait.edit_text("😕 Не получилось прочитать чек. Попробуй сфотографировать ровнее "
                                    "или введи сумму вручную: <code>12,50 кофе</code>")
    cat = db.category(r["category_id"])
    await state.set_state(Tx.category)
    await state.update_data(kind="expense", amount=r["amount"], comment=r["merchant"],
                            when=r["when"], cat_id=cat["id"])
    lines = [f"🧾 <b>Чек</b>", "",
             f"➖ <b>{db.fmt(r['amount'])}</b>",
             f"🏪 {html.escape(r['merchant'] or '—')}",
             f"📅 {r['date_label']}",
             f"{cat['emoji']} {cat['name']}"]
    if r.get("note"):
        lines += ["", f"<i>{html.escape(r['note'])}</i>"]
    await wait.edit_text("\n".join(lines), reply_markup=receipt_kb())


@router.callback_query(F.data.startswith("rcpt:"))
async def receipt_action(cb: CallbackQuery, state: FSMContext, bot: Bot):
    action = cb.data.split(":")[1]
    data = await state.get_data()
    if "amount" not in data:
        await cb.message.edit_text("Чек устарел — пришли фото ещё раз.")
        return await cb.answer()
    if action == "save":
        return await save_tx(cb, bot, state, data, db.category(data["cat_id"]))
    if action == "cat":
        await cb.message.edit_reply_markup(reply_markup=cat_keyboard("expense"))
        return await cb.answer("Выбери категорию")
    if action == "amount":
        await state.set_state(Tx.amount)
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.message.answer("Какая сумма? Например <code>47,30</code>")
        return await cb.answer()


# ---------- отчёты ----------

async def send_report(msg: Message, month: str):
    s = db.month_summary(month)
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ " + reports.month_title(db.prev_month_key(month)),
              callback_data=f"rep:{db.prev_month_key(month)}")
    if month != db.month_key():
        kb.button(text="Текущий месяц ▶️", callback_data=f"rep:{db.month_key()}")
    png = reports.chart_png(s)
    if png:
        await msg.answer_photo(BufferedInputFile(png, f"budget-{month}.png"))
    await msg.answer(reports.text_report(s), reply_markup=kb.as_markup())


@router.message(F.text == B_REP)
@router.message(Command("report"))
async def report(msg: Message, state: FSMContext):
    await state.clear()
    await send_report(msg, db.month_key())


@router.callback_query(F.data.startswith("rep:"))
async def report_month(cb: CallbackQuery):
    await cb.answer()
    await send_report(cb.message, cb.data.split(":")[1])


# ---------- история ----------

@router.message(F.text == B_HIST)
async def history(msg: Message, state: FSMContext):
    await state.clear()
    rows = db.recent(10)
    if not rows:
        return await msg.answer("Записей пока нет.")
    lines, kb = ["🧾 <b>Последние записи</b>", ""], InlineKeyboardBuilder()
    for i, r in enumerate(rows, 1):
        sign = "➖" if r["kind"] == "expense" else "➕"
        day = r["created_at"][8:10] + "." + r["created_at"][5:7]
        note = f" · {html.escape(r['comment'])}" if r["comment"] else ""
        lines.append(f"{i}. {day} {sign}{db.fmt(r['amount'])} {r['emoji']} {r['cat_name']}{note} — {html.escape(r['user_name'] or '?')}")
        kb.button(text=f"🗑 {i}", callback_data=f"undo:{r['id']}")
    kb.adjust(5)
    await msg.answer("\n".join(lines) + "\n\nНажми номер, чтобы удалить запись.", reply_markup=kb.as_markup())


# ---------- бюджет и лимиты ----------

def budget_text() -> str:
    month = db.month_key()
    lim = db.limits()
    lines = [f"💰 <b>Лимиты на месяц</b> · {reports.month_title(month)}", ""]
    if not lim:
        lines.append("Лимиты пока не заданы. Выбери категорию ниже или «Весь бюджет».")
    for cid, amount in sorted(lim.items()):
        label = "Весь бюджет" if cid == db.TOTAL else f"{db.category(cid)['emoji']} {db.category(cid)['name']}"
        sp = db.spent(month, cid)
        p = sp / amount
        left = f"осталось {db.fmt(amount - sp)}" if sp <= amount else f"перерасход {db.fmt(sp - amount)}"
        lines.append(f"<b>{label}</b>: {db.fmt(sp)} из {db.fmt(amount)}\n{reports.bar(p)} {p:.0%} · {left}")
    return "\n".join(lines)


@router.message(F.text == B_BUD)
async def budget(msg: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="💰 Весь бюджет", callback_data=f"lim:{db.TOTAL}")
    for c in db.categories("expense"):
        kb.button(text=f"{c['emoji']} {c['name']}", callback_data=f"lim:{c['id']}")
    kb.adjust(1, 2)
    await msg.answer(budget_text() + "\n\n<i>Изменить лимит:</i>", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("lim:"))
async def limit_pick(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[1])
    await state.set_state(Lim.amount)
    await state.update_data(cat_id=cid)
    label = "весь бюджет" if cid == db.TOTAL else db.category(cid)["name"]
    await cb.message.answer(f"Лимит на месяц для «{label}»? Сумма в евро, <code>0</code> — убрать лимит.")
    await cb.answer()


@router.message(Lim.amount, F.text, NOT_MENU)
async def limit_set(msg: Message, state: FSMContext):
    text = msg.text.strip()
    parsed = (0, "") if text == "0" else db.parse_amount(text)
    if parsed is None:
        return await msg.answer("Нужна сумма, например <code>600</code>, или <code>0</code>.")
    cid = (await state.get_data())["cat_id"]
    db.set_limit(cid, parsed[0])
    await state.clear()
    await msg.answer(budget_text(), reply_markup=MENU)


# ---------- категории ----------

@router.message(F.text == B_CATS)
async def cats(msg: Message, state: FSMContext):
    await state.clear()
    exp = ", ".join(f"{c['emoji']} {c['name']}" for c in db.categories("expense"))
    inc = ", ".join(f"{c['emoji']} {c['name']}" for c in db.categories("income"))
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Категория расходов", callback_data="addcat:expense")
    kb.button(text="➕ Категория доходов", callback_data="addcat:income")
    kb.button(text="🗑 Удалить категорию", callback_data="delmenu")
    kb.adjust(1)
    await msg.answer(f"🗂 <b>Расходы:</b> {exp}\n\n<b>Доходы:</b> {inc}", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("addcat:"))
async def addcat(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Cat.name)
    await state.update_data(kind=cb.data.split(":")[1])
    await cb.message.answer("Название новой категории, можно с эмодзи впереди: <code>🐶 Собака</code>")
    await cb.answer()


@router.message(Cat.name, F.text, NOT_MENU)
async def addcat_name(msg: Message, state: FSMContext):
    text = re.sub(r"[<>&]", "", msg.text).strip()[:40]
    if not text:
        return await msg.answer("Пустое название, попробуй ещё раз.")
    first, _, rest = text.partition(" ")
    emoji, name = (first, rest.strip()) if rest and not first[0].isalnum() else ("•", text)
    kind = (await state.get_data())["kind"]
    db.add_category(kind, emoji, name)
    await state.clear()
    await msg.answer(f"Добавлено: {emoji} {name}", reply_markup=MENU)


@router.callback_query(F.data == "delmenu")
async def delmenu(cb: CallbackQuery):
    kb = InlineKeyboardBuilder()
    for kind, sign in (("expense", "➖"), ("income", "➕")):
        for c in db.categories(kind):
            kb.button(text=f"{sign} {c['emoji']} {c['name']}", callback_data=f"delcat:{c['id']}")
    kb.adjust(2)
    await cb.message.answer("Какую скрыть? Старые записи останутся в отчётах.", reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("delcat:"))
async def delcat(cb: CallbackQuery):
    cat = db.category(int(cb.data.split(":")[1]))
    db.hide_category(cat["id"])
    await cb.message.edit_text(f"Категория «{cat['name']}» удалена.")
    await cb.answer()


# ---------- экспорт ----------

@router.message(Command("export"))
async def export(msg: Message):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Дата", "Тип", "Сумма, €", "Категория", "Кто", "Комментарий"])
    for r in db.export_rows():
        w.writerow([r["created_at"].replace("T", " "), "Расход" if r["kind"] == "expense" else "Доход",
                    f"{r['amount'] / 100:.2f}".replace(".", ","), r["category"], r["user"], r["comment"]])
    data = ("﻿" + buf.getvalue()).encode("utf-8")  # BOM — чтобы Excel понял кириллицу
    await msg.answer_document(BufferedInputFile(data, f"budget-{db.month_key()}.csv"))


# ---------- быстрый ввод: просто сумма ----------

@router.message(StateFilter(None, Tx.category), F.text.regexp(r"^\s*[+\-]?\s*€?\s*\d"))
async def quick(msg: Message, state: FSMContext):
    await state.clear()
    text = msg.text.strip()
    kind = "income" if text.startswith("+") else "expense"
    parsed = db.parse_amount(text.lstrip("+-"))
    if not parsed:
        return await msg.answer("Не понял сумму. Пример: <code>12,50 кофе</code>")
    await ask_category(msg, state, kind, *parsed)


@router.message(StateFilter(None))
async def fallback(msg: Message):
    await msg.answer("Напиши сумму (<code>12,50 кофе</code>) или выбери действие в меню.", reply_markup=MENU)


async def main():
    db.init()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(AccessMiddleware())
    dp.callback_query.outer_middleware(AccessMiddleware())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот запущен, доступ: %s", ALLOWED or "никому (заполни ALLOWED_IDS)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
