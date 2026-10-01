"""Comparable SEC tables; never combine currencies, concepts or periods."""
from collections import Counter, defaultdict
from decimal import Decimal
from .material_reading import fact_rows, CORE_FACTS
from .number_display import CURRENCIES


def fact_tables(data, frequency="annual"):
    groups = defaultdict(list)
    for row in fact_rows(data, frequency):
        if not row["end"]:
            continue
        category = "每股数据" if row["currency"].endswith("/shares") else "年度经营数据" if row["start"] else "年末资产与负债"
        if frequency == "quarterly":
            category = (row["duration"] + ("每股数据" if row["currency"].endswith("/shares") else "经营数据")) if row["start"] else "期末资产与负债"
        groups[(row["standard"], row["currency"], category)].append(row)
    tables = []
    order = {code: i for i, code in enumerate(CORE_FACTS)}
    for (standard, unit, category), values in sorted(groups.items()):
        # Exact start/end pairs keep unusual fiscal periods separate.
        periods = sorted({(r["start"], r["end"]) for r in values}, key=lambda p: (p[1], p[0]), reverse=True)[:3]
        values = [r for r in values if (r["start"], r["end"]) in periods]
        codes = sorted({r["code"] for r in values}, key=lambda c: order[c])
        counts = Counter(CORE_FACTS[c] for c in codes)
        ranks = Counter()
        currency = unit.split("/")[0]
        name = CURRENCIES.get(currency, currency)
        largest = max((abs(r["value"]) for r in values if r["value"] is not None), default=Decimal(0))
        if unit.endswith("/shares"):
            scale, display_unit = Decimal(1), name + "/股"
        elif largest >= Decimal("100000000"):
            scale, display_unit = Decimal("100000000"), "亿" + name
        elif largest >= Decimal("10000"):
            scale, display_unit = Decimal("10000"), "万" + name
        else:
            scale, display_unit = Decimal(1), name
        rows = []
        for code in codes:
            label = CORE_FACTS[code]
            ranks[label] += 1
            title = label + (f" · 口径 {ranks[label]}" if counts[label] > 1 else "")
            lookup = {(r["start"], r["end"]): r for r in values if r["code"] == code}
            cells = []
            for period in periods:
                source = lookup.get(period)
                amount = source["value"] if source else None
                cells.append({"amount": f"{amount / scale:,.2f}" if amount is not None else "—",
                              "source": source})
            rows.append({"label": title, "code": code, "source_label": next(r["source_label"] for r in values if r["code"] == code), "cells": cells})
        tables.append({"title": category, "standard": standard, "currency": currency,
                       "unit": display_unit, "rows": rows, "has_alternatives": any(v > 1 for v in counts.values()),
                       "periods": [{"year": end[:4] if frequency == "annual" else end, "start": start, "end": end,
                                    "filed": max((r["filed"] or "" for r in values if (r["start"], r["end"]) == (start, end)), default="")}
                                   for start, end in periods]})
    category_order = {"年度经营数据": 0, "年末资产与负债": 1, "每股数据": 2}
    if frequency == "quarterly":
        category_order = {title: i for i, title in enumerate(("单季经营数据", "单季每股数据", "期末资产与负债", "半年累计经营数据", "半年累计每股数据", "九个月累计经营数据", "九个月累计每股数据"))}
    return sorted(tables, key=lambda t: (t["standard"], t["currency"], category_order.get(t["title"], 3), t["title"]))
