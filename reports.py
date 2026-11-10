"""Текстовый отчёт и график за месяц."""
import calendar
import html
import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from db import TOTAL, fmt  # noqa: E402

MONTHS = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль",
          "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]

INK, MUTED, GRID = "#1f2328", "#6e7781", "#e6e8eb"
BAR, OVER, LIMIT = "#3b6fd8", "#d1495b", "#9aa3ad"


def month_title(key: str) -> str:
    y, m = map(int, key.split("-"))
    return f"{MONTHS[m]} {y}"


def bar(pct: float, width: int = 10) -> str:
    filled = min(width, round(pct * width))
    return "▰" * filled + "▱" * (width - filled)


def text_report(s: dict) -> str:
    lines = [f"📊 <b>{month_title(s['month'])}</b>", ""]
    balance = s["income_total"] - s["expense_total"]
    lines += [
        f"➕ Доходы: <b>{fmt(s['income_total'])}</b>",
        f"➖ Расходы: <b>{fmt(s['expense_total'])}</b>",
        f"{'🟢' if balance >= 0 else '🔴'} Баланс: <b>{fmt(balance) if balance >= 0 else '−' + fmt(-balance)}</b>",
    ]
    lim = s["limits"]
    if TOTAL in lim:
        pct = s["expense_total"] / lim[TOTAL]
        lines.append(f"💰 Бюджет: {bar(pct)} {pct:.0%} из {fmt(lim[TOTAL])}")

    if s["expenses"]:
        lines += ["", "<b>Расходы по категориям</b>"]
        for r in s["expenses"]:
            share = r["total"] / s["expense_total"]
            line = f"{r['emoji']} {r['name']} — {fmt(r['total'])} ({share:.0%})"
            if r["id"] in lim:
                p = r["total"] / lim[r["id"]]
                line += f"\n    {bar(p, 8)} {p:.0%} лимита{' ⚠️' if p >= 1 else ''}"
            lines.append(line)
    if s["incomes"]:
        lines += ["", "<b>Доходы</b>"]
        lines += [f"{r['emoji']} {r['name']} — {fmt(r['total'])}" for r in s["incomes"]]
    if len(s["by_user"]) > 1:
        lines += ["", "<b>Кто сколько потратил</b>"]
        lines += [f"👤 {html.escape(str(r['name']))} — {fmt(r['total'])}" for r in s["by_user"]]
    if not s["expenses"] and not s["incomes"]:
        lines += ["", "Пока нет записей за этот месяц."]
    return "\n".join(lines)


def chart_png(s: dict) -> bytes | None:
    if not s["expenses"]:
        return None
    lim = s["limits"]
    exp = s["expenses"][:10][::-1]  # сверху самая крупная
    y, m = map(int, s["month"].split("-"))
    days = calendar.monthrange(y, m)[1]

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "text.color": INK,
                         "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": INK})
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(7, 8.5), height_ratios=[1.3, 1])
    fig.patch.set_facecolor("white")

    # 1. Категории
    names = [r["name"] for r in exp]
    vals = [r["total"] / 100 for r in exp]
    colors = [OVER if r["id"] in lim and r["total"] >= lim[r["id"]] else BAR for r in exp]
    bars = ax1.barh(names, vals, color=colors, height=0.6)
    for r, b in zip(exp, bars):
        if r["id"] in lim:
            ax1.plot([lim[r["id"]] / 100] * 2, [b.get_y() - 0.08, b.get_y() + b.get_height() + 0.08],
                     color=LIMIT, lw=2)
            ax1.text(lim[r["id"]] / 100, b.get_y() + b.get_height() + 0.12, "лимит",
                     ha="center", va="bottom", fontsize=7, color=MUTED)
        ax1.text(b.get_width(), b.get_y() + b.get_height() / 2, "  " + fmt(r["total"]),
                 va="center", fontsize=9, color=INK)
    ax1.set_title("Расходы по категориям", loc="left", fontsize=12, fontweight="bold", pad=10)
    ax1.set_xlim(0, max(vals + [lim[r["id"]] / 100 for r in exp if r["id"] in lim]) * 1.3)
    ax1.xaxis.set_visible(False)

    # 2. Накопительные траты по дням
    xs = list(range(1, days + 1))
    acc, cum = 0, []
    for d in xs:
        acc += s["daily"].get(d, 0)
        cum.append(acc / 100)
    last_day = max(s["daily"]) if s["daily"] else 1
    ax2.fill_between(xs[:last_day], cum[:last_day], color=BAR, alpha=0.15)
    ax2.plot(xs[:last_day], cum[:last_day], color=BAR, lw=2, label="Потрачено")
    if TOTAL in lim:
        t = lim[TOTAL] / 100
        ax2.plot([1, days], [t / days, t], color=LIMIT, lw=1.5, ls="--", label="Равномерный темп")
        ax2.axhline(t, color=OVER, lw=1, ls=":", label=f"Бюджет {fmt(lim[TOTAL])}")
        ax2.legend(frameon=False, fontsize=9, loc="upper left")
    ax2.set_title("Накопленные расходы за месяц, €", loc="left", fontsize=12, fontweight="bold", pad=10)
    ax2.set_xlim(1, days)
    ax2.set_ylim(bottom=0)
    ax2.set_xlabel("День месяца")
    ax2.grid(axis="y", color=GRID)

    for ax in (ax1, ax2):
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.spines["left"].set_color(GRID)
        ax.spines["bottom"].set_color(GRID)

    fig.suptitle(month_title(s["month"]), x=0.02, ha="left", fontsize=14, fontweight="bold")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    return buf.getvalue()
