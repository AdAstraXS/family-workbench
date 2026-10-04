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


def planned_events(start, end, country=""):
    return [e.copy() for e in schedule()["events"] if start <= e["day"] <= end and (not country or e["country"] == country)]
