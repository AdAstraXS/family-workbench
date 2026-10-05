"""Verified official schedule snapshot; rendering never contacts a source."""
import json
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo


@lru_cache(maxsize=1)
def schedule():
    data = json.loads(Path(__file__).with_name("release_schedule.json").read_text(encoding="utf-8"))
    sources = {s["code"]: s for s in data["sources"]}
    events = []
    for item in data["events"]:
        local = datetime.combine(date.fromisoformat(item["date"]), datetime.strptime(item["time"], "%H:%M").time(),
                                 tzinfo=ZoneInfo(item["timezone"]))
        beijing = local.astimezone(ZoneInfo("Asia/Shanghai"))
        events.append({**item, "when": beijing, "day": beijing.date(), "source": sources[item["agency"]],
                       "local_time": local.strftime("%Y-%m-%d %H:%M %Z"), "kind": "planned"})
    return {**data, "events": sorted(events, key=lambda e: (e["when"], e["title"], e["period"]))}


def current_schedule():
    from .models import MacroCalendarSnapshot
    original = schedule()
    sources = {s["code"]: s.copy() for s in original["sources"]}
    events = list(original["events"])
    latest = {}
    for snapshot in MacroCalendarSnapshot.objects.order_by("agency", "-checked_at", "-pk"):
        latest.setdefault(snapshot.agency, snapshot)
    for agency, snapshot in latest.items():
        source = sources[agency]
        source.update(url=snapshot.source_url, verified=snapshot.checked_at.strftime("%Y-%m-%d %H:%M"), sha256=snapshot.content_hash)
        events = [e for e in events if e["agency"] != agency]
        for item in snapshot.payload["events"]:
            local = datetime.fromisoformat(item["date"] + "T" + item["time"]).replace(tzinfo=ZoneInfo(item["timezone"]))
            beijing = local.astimezone(ZoneInfo("Asia/Shanghai"))
            events.append({**item, "when": beijing, "day": beijing.date(), "source": source,
                           "local_time": local.strftime("%Y-%m-%d %H:%M %Z"), "kind": "planned"})
    return {**original, "sources": list(sources.values()), "events": sorted(events, key=lambda e: (e["when"], e["title"])),
            "years": sorted({e["day"].year for e in events})}


def planned_events(start, end, country=""):
    return [e.copy() for e in current_schedule()["events"] if start <= e["day"] <= end and (not country or e["country"] == country)]
