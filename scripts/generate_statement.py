#!/usr/bin/env python3
"""
Генератор реалистичной банковской выписки (PDF) для демо на вебинаре.

Поток:  CSV (250 транзакций)
      → сортировка по дате + бегущий баланс
      → группировка по категориям
      → рендер Jinja2-шаблона в HTML
      → headless Google Chrome печатает HTML в PDF

Полностью локально: без интернета, без токенов, без внешних SaaS.

Использование:
    python3 scripts/generate_statement.py

Параметры по умолчанию (поправимо через CLI):
    --csv          test_bank_statement_250.csv
    --out-pdf      deploy/sample-bank-statement.pdf
    --out-html     deploy/sample-bank-statement.html
"""

import argparse
import csv
import os
import subprocess
import sys
from collections import OrderedDict
from datetime import datetime

from jinja2 import Environment, FileSystemLoader

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# --- Параметры «банка» (вымышленные, для демо) ---
BANK = {
    "name": "ProFinance Bank",
    "logo": "P",
    "color": "#1e3a8a",
    "address": "вул. Хрещатик 22, Київ",
    "license": "4821",
}
ACCOUNT = {
    "holder": "ФОП Демо Бізнес",
    "iban": "UA12 3456 7890 0000 0000 0000 12345",  # 27 цифр після UA (валідний)
    "currency": "UAH",
}


def fmt(value: float) -> str:
    """Число → строка в украинском формате: 1 234 567,89"""
    s = f"{abs(value):,.2f}"
    s = s.replace(",", " ").replace(".", ",")
    return f"-{s}" if value < 0 else s


def parse_amount(raw: str) -> float:
    """'−6 846,19' / '-6846,19' / '4649,33' → float."""
    return float(raw.strip().strip('"').replace(" ", "").replace(",", "."))


def parse_date(raw: str):
    """'02.12.2025' → datetime."""
    return datetime.strptime(raw.strip(), "%d.%m.%Y")


def load_rows(csv_path: str):
    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    parsed = []
    for r in rows:
        amt = parse_amount(r["Сума"])
        parsed.append({
            "date_obj": parse_date(r["Дата"]),
            "date": r["Дата"].strip(),
            "description": r["Опис"].strip(),
            "category": r["Категорія"].strip(),
            "amount": amt,
        })
    # сортировка по дате (стабильно), затем по описанию для предсказуемости
    parsed.sort(key=lambda x: (x["date_obj"], x["description"]))
    return parsed


def compute(rows, opening_balance: float):
    """Считает бегущий баланс и итоги. Возвращает (transactions, totals).

    Намеренно НЕ группирует по категориям: справжня виписка містить лише сирі
    операції (дата, опис, сума, залишок). Рознесення по категоріях — це робота
    AI-агента у n8n-воркфлоу, а не самої виписки.
    """

    transactions = []
    balance = opening_balance
    for r in rows:
        amt = r["amount"]
        balance += amt
        transactions.append({
            "date": r["date"],
            "description": r["description"],
            "income": fmt(amt) if amt > 0 else "",
            "expense": fmt(amt) if amt < 0 else "",
            "balance": fmt(balance),
            "_amt": amt,
        })

    total_income = sum(t["_amt"] for t in transactions if t["_amt"] > 0)
    total_expense = sum(abs(t["_amt"]) for t in transactions if t["_amt"] < 0)
    final_balance = opening_balance + total_income - total_expense

    for t in transactions:
        del t["_amt"]  # служебное поле не нужно в шаблоне

    totals = {
        "total_income": total_income,
        "total_expense": total_expense,
        "final_balance": final_balance,
        "opening_balance": opening_balance,
        "count": len(transactions),
    }
    return transactions, totals


def render_html(transactions, totals, template_path, out_html):
    env = Environment(loader=FileSystemLoader(os.path.dirname(template_path)),
                      autoescape=True, keep_trailing_newline=True)
    tmpl = env.get_template(os.path.basename(template_path))

    period_text = (f"{transactions[0]['date']} – {transactions[-1]['date']}"
                   if transactions else "—")

    html = tmpl.render(
        bank=BANK,
        account=ACCOUNT,
        statement_no=f"VS-{datetime.now().strftime('%Y%m%d')}-001",
        generated_at=datetime.now().strftime("%d.%m.%Y %H:%M"),
        period_text=period_text,
        opening_balance_str=fmt(totals["opening_balance"]),
        total_income_str=fmt(totals["total_income"]),
        total_expense_str=fmt(totals["total_expense"]),
        final_balance_str=fmt(totals["final_balance"]),
        transactions=transactions,
    )

    os.makedirs(os.path.dirname(out_html), exist_ok=True)
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html)
    return out_html


def chrome_print(html_path: str, pdf_path: str):
    """Печатает HTML в PDF через headless Chrome."""
    os.makedirs(os.path.dirname(pdf_path), exist_ok=True)
    file_url = "file://" + os.path.abspath(html_path)
    cmd = [
        CHROME,
        "--headless=new",
        "--disable-gpu",
        "--no-pdf-header-footer",
        f"--print-to-pdf={os.path.abspath(pdf_path)}",
        file_url,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if res.returncode != 0 or not os.path.exists(pdf_path):
        sys.stderr.write(
            f"Chrome failed (rc={res.returncode}):\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}\n"
        )
        sys.exit(1)
    return pdf_path


def main():
    ap = argparse.ArgumentParser(description="Сгенерировать банковскую выписку PDF")
    ap.add_argument("--csv", default=os.path.join(REPO_ROOT, "test_bank_statement_250.csv"))
    ap.add_argument("--template", default=os.path.join(REPO_ROOT, "scripts", "statement_template.html"))
    ap.add_argument("--out-html", default=os.path.join(REPO_ROOT, "deploy", "sample-bank-statement.html"))
    ap.add_argument("--out-pdf", default=os.path.join(REPO_ROOT, "deploy", "sample-bank-statement.pdf"))
    ap.add_argument("--opening-balance", type=float, default=1800000.0,
                    help="Начальный баланс счёта, UAH. По умолч. 1800000 — "
                         "подобран так, чтобы бегущий баланс за период оставался "
                         "положительным (мин ~150k), как у реального рабочего счёта.")
    args = ap.parse_args()

    if not os.path.exists(CHROME):
        sys.exit(f"Google Chrome не найден: {CHROME}")

    print(f"[1/4] Читаю CSV: {args.csv}")
    rows = load_rows(args.csv)
    print(f"      {len(rows)} транзакций ({rows[0]['date']} – {rows[-1]['date']})")

    print("[2/4] Считаю бегущий баланс и итоги")
    transactions, totals = compute(rows, args.opening_balance)
    print(f"      дохід {fmt(totals['total_income'])} · витрата {fmt(totals['total_expense'])} "
          f"· кінцевий {fmt(totals['final_balance'])} · {totals['count']} операцій")

    print(f"[3/4] Рендерю HTML: {args.out_html}")
    render_html(transactions, totals, args.template, args.out_html)

    print(f"[4/4] Печатаю PDF через headless Chrome: {args.out_pdf}")
    chrome_print(args.out_html, args.out_pdf)

    size_kb = os.path.getsize(args.out_pdf) / 1024
    print(f"\n✓ Готово: {args.out_pdf}  ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
