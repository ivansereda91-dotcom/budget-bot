"""Чтение чеков по фото через Claude (vision)."""
import base64
import json
import logging
import os
import re
from datetime import date, timedelta

import db

MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
MONTHS = ["", "января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]

_client = None


class NotAReceipt(Exception):
    pass


def enabled() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def client():
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic
        _client = AsyncAnthropic()
    return _client


def build_prompt(categories) -> str:
    cats = "\n".join(f"{c['id']}: {c['name']}" for c in categories)
    return (
        "На фото — кассовый чек (скорее всего французский). Извлеки данные и ответь ТОЛЬКО одним JSON-объектом:\n"
        '{"is_receipt": true, "total": 47.30, "currency": "EUR", "merchant": "Carrefour", '
        '"date": "2026-11-10", "category_id": 1, "note": ""}\n\n'
        "Правила:\n"
        "- total — ИТОГОВАЯ сумма к оплате (TOTAL / NET À PAYER / MONTANT TTC), не промежуточная и не сумма НДС;\n"
        "- merchant — короткое название магазина или заведения;\n"
        "- date — дата покупки YYYY-MM-DD или null, если не видно;\n"
        "- category_id — самая подходящая категория из списка;\n"
        "- note — по-русски, только если есть сомнение (например, сумма плохо читается), иначе пустая строка;\n"
        '- если на фото не чек — {"is_receipt": false, "note": "что на фото, по-русски"}.\n\n'
        f"Категории:\n{cats}"
    )


def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    a, b = text.find("{"), text.rfind("}")
    if a == -1 or b == -1:
        raise ValueError("В ответе нет JSON")
    return json.loads(text[a:b + 1])


def normalize(d: dict, categories, today: date | None = None) -> dict:
    """Проверяет и приводит ответ модели к данным для записи."""
    today = today or db.now().date()
    if not d.get("is_receipt", True):
        raise NotAReceipt(d.get("note") or "Похоже, это не чек.")
    total = d.get("total")
    if isinstance(total, str):
        total = total.replace(",", ".").replace("€", "").replace(" ", "")
    amount = round(float(total) * 100)
    if not 0 < amount <= 100_000_00:
        raise ValueError(f"Странная сумма: {total}")

    ids = {c["id"] for c in categories}
    other = next((c["id"] for c in categories if c["name"] == "Другое"), categories[-1]["id"])
    try:
        cat_id = int(d.get("category_id"))
    except (TypeError, ValueError):
        cat_id = other
    if cat_id not in ids:
        cat_id = other

    when, label = None, "сегодня"
    try:
        dd = date.fromisoformat(str(d.get("date")))
        if today - timedelta(days=60) <= dd < today:  # прошлые дни — записываем на дату чека
            when = dd.isoformat()
            label = "вчера" if dd == today - timedelta(days=1) else f"{dd.day} {MONTHS[dd.month]}"
    except ValueError:
        pass

    note = (d.get("note") or "").strip()
    cur = (d.get("currency") or "EUR").upper()
    if cur not in ("EUR", "€"):
        note = (note + " " if note else "") + f"Валюта чека: {cur} — сумма записана как есть."
    return {"amount": amount, "merchant": (d.get("merchant") or "").strip()[:40],
            "category_id": cat_id, "when": when, "date_label": label, "note": note}


async def parse(image: bytes, mime: str, categories) -> dict:
    if len(image) > 4_500_000:
        raise NotAReceipt("Файл слишком большой — пришли чек обычным фото, не файлом.")
    if mime not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        mime = "image/jpeg"
    msg = await client().messages.create(
        model=MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": mime,
                                         "data": base64.b64encode(image).decode()}},
            {"type": "text", "text": build_prompt(categories)},
        ]}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    try:
        return normalize(parse_json(text), categories)
    except (NotAReceipt, ValueError):
        logging.warning("Чек: stop=%s ответ=%s", msg.stop_reason, text[:300].replace("\n", " "))
        raise
