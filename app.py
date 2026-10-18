import json
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Acme Appliances Ledger Agent")

ORDERS = json.loads(
    (Path(__file__).parent / "cleaned_orders.json").read_text(encoding="utf-8")
)

PRODUCTS = sorted({o["product"] for o in ORDERS}, key=len, reverse=True)
REGIONS = sorted({o["region"] for o in ORDERS}, key=len, reverse=True)

MONTHS = {
    "january": 1, "february": 2, "march": 3,
    "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
}

MONTH_PATTERN = re.compile(
    r"\b(" + "|".join(MONTHS) + r")\b", re.I
)


class QuestionRequest(BaseModel):
    question: str


def money(value):
    return float(
        Decimal(value).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )


def extract_dates(q):
    years = re.findall(r"\b20\d{2}\b", q)
    year = int(years[0]) if years else 2026

    explicit_dates = re.findall(
        r"\b20\d{2}-\d{2}-\d{2}\b", q
    )

    if len(explicit_dates) >= 2:
        return date.fromisoformat(explicit_dates[0]), date.fromisoformat(explicit_dates[1])

    if len(explicit_dates) == 1:
        d = date.fromisoformat(explicit_dates[0])
        return d, d

    months = [MONTHS[m.lower()] for m in MONTH_PATTERN.findall(q)]

    if len(months) >= 2 and re.search(
        r"\b(from|between|through|until|to)\b", q
    ):
        start_month, end_month = months[:2]
        start = date(year, start_month, 1)

        end_year = year + (1 if end_month < start_month else 0)
        if end_month == 12:
            end = date(end_year, 12, 31)
        else:
            end = date.fromordinal(
                date(end_year, end_month + 1, 1).toordinal() - 1
            )

        return start, end

    if months:
        month = months[0]
        start = date(year, month, 1)

        if month == 12:
            end = date(year, 12, 31)
        else:
            end = date.fromordinal(
                date(year, month + 1, 1).toordinal() - 1
            )

        return start, end

    quarter = re.search(r"\bq([1-4])\b|\b([1-4])(?:st|nd|rd|th)? quarter\b", q)

    if quarter:
        number = int(quarter.group(1) or quarter.group(2))
        first = (number - 1) * 3 + 1
        last = first + 2

        start = date(year, first, 1)
        if last == 12:
            end = date(year, 12, 31)
        else:
            end = date.fromordinal(
                date(year, last + 1, 1).toordinal() - 1
            )

        return start, end

    if years:
        return date(year, 1, 1), date(year, 12, 31)

    return None, None


def get_filters(question):
    q = question.lower()

    product = next(
        (p for p in PRODUCTS if re.search(
            r"\b" + re.escape(p.lower()) + r"s?\b", q
        )),
        None,
    )

    region = next(
        (r for r in REGIONS if re.search(
            r"\b" + re.escape(r.lower()) + r"\b", q
        )),
        None,
    )

    customer_match = re.search(r"\bc\d{4}\b", q)
    customer = customer_match.group().upper() if customer_match else None

    start, end = extract_dates(q)

    return product, region, customer, start, end


def matching_orders(question, ranking_field=None):
    product, region, customer, start, end = get_filters(question)

    result = []

    for order in ORDERS:
        d = date.fromisoformat(order["business_date"])

        if product and ranking_field != "product" and order["product"] != product:
            continue

        if region and ranking_field != "region" and order["region"] != region:
            continue

        if customer and ranking_field != "customer" and order["customer"] != customer:
            continue

        if start and d < start:
            continue

        if end and d > end:
            continue

        result.append(order)

    return result


def get_metric(q):
    if re.search(r"\b(refund rate|refund percentage|percentage refunded)\b", q):
        return "refund_rate"

    if re.search(r"\b(average|mean)\b", q) and re.search(r"\b(order|purchase|transaction|revenue)\b", q):
        return "average"

    if re.search(r"\b(void|voided|cancelled|canceled)\b", q):
        return "void_count"

    if re.search(
        r"\b(refund|refunded|refunds)\b", q
    ):
        if re.search(
            r"\b(amount|value|total usd|total dollars|revenue|money|how much|worth)\b",
            q,
        ):
            return "refund_amount"
        return "refund_count"

    if re.search(r"\b(units|quantity|items sold|pieces sold)\b", q):
        return "units"

    if re.search(r"\b(unique|distinct|different)\s+(?:paying\s+)?customers?\b|\b(how many|number of|count of)\s+(?:different\s+|unique\s+|distinct\s+|paying\s+|paid\s+)*customers?\b", q):
        return "customers"

    if re.search(
        r"\b(how many|number of|count of|count the)\b", q
    ) and re.search(r"\b(orders|purchases|transactions)\b", q):
        return "orders"

    return "revenue"


def calculate(orders, metric, q=""):
    paid = [o for o in orders if o["status"] == "paid"]
    refunded = [o for o in orders if o["status"] == "refunded"]

    if metric == "void_count":
        return sum(o["status"] == "void" for o in orders)

    if metric == "refund_count":
        return len(refunded)

    if metric == "refund_amount":
        return money(sum(
            (Decimal(o["amount_usd"]) for o in refunded),
            Decimal("0"),
        ))

    if metric == "refund_rate":
        denominator = len(paid) + len(refunded)
        return money(Decimal(len(refunded)) * 100 / denominator) if denominator else 0

    if metric == "units":
        return sum(int(o["qty"]) for o in paid)

    if metric == "customers":
        return len({o["customer"] for o in paid})

    if metric == "orders":
        if re.search(r"\b(all orders|total orders placed|orders placed)\b", q):
            return len(orders)
        return len(paid)

    revenue = sum(
        (Decimal(o["amount_usd"]) for o in paid),
        Decimal("0"),
    )

    if metric == "average":
        return money(revenue / len(paid)) if paid else 0

    return money(revenue)


def get_ranking_field(q):
    if re.search(r"\b(which|what|top|best|worst)\s+customers?\b", q):
        return "customer"

    if re.search(r"\b(which|what|top|best|worst)\s+regions?\b", q):
        return "region"

    if re.search(r"\b(which|what|top|best|worst)\s+products?\b", q):
        return "product"

    if re.search(r"\bcustomer\b", q) and re.search(
        r"\b(most|least|highest|lowest|top|biggest)\b", q
    ):
        return "customer"

    if re.search(r"\bregion\b", q) and re.search(
        r"\b(most|least|highest|lowest|top|biggest)\b", q
    ):
        return "region"

    if re.search(r"\bproduct\b", q) and re.search(
        r"\b(most|least|highest|lowest|top|biggest|best-selling)\b", q
    ):
        return "product"

    return None


def answer_question(question):
    q = question.lower().strip()
    metric = get_metric(q)
    ranking_field = get_ranking_field(q)

    if ranking_field:
        orders = matching_orders(question, ranking_field)
        groups = defaultdict(list)

        for order in orders:
            groups[order[ranking_field]].append(order)

        if not groups:
            return None

        reverse = not bool(re.search(
            r"\b(least|lowest|fewest|worst|smallest)\b", q
        ))

        ranked = sorted(
            (
                (name, calculate(rows, metric, q))
                for name, rows in groups.items()
            ),
            key=lambda x: (-x[1] if reverse else x[1], x[0]),
        )

        return ranked[0][0]

    orders = matching_orders(question)
    return calculate(orders, metric, q)


@app.get("/")
def health():
    return {"status": "ok", "orders": len(ORDERS)}


@app.post("/")
def ask(request: QuestionRequest):
    return {"answer": answer_question(request.question)}



