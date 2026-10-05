"""Read-only host evidence. Never deletes backups or changes DSM schedules."""
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

TASKS = {16: ("official", [(11, 20), (18, 20)], "每天11:20、18:20"),
         17: ("structured", [(6, 40)], "每天06:40"), 18: ("calendar", [(7, 40)], "每天07:40")}
BACKUP_NAME = re.compile(r"family-workbench-macro-(official|structured|calendar)-(\d{8}-\d{6})\.dump")


def backup_inventory(directory, now=None):
    now = now or datetime.now(timezone.utc)
    files = []
    for path in Path(directory).glob("family-workbench-macro-*.dump"):
        match = BACKUP_NAME.fullmatch(path.name)
        if not match or path.is_symlink() or not path.is_file():
            continue
        stat = path.stat()
        if stat.st_size:
            files.append((path.name, match[1], datetime.fromtimestamp(stat.st_mtime, timezone.utc), stat.st_size))
    files.sort(key=lambda item: item[2])
    keep, months, groups = set(), set(), set()
    for name, group, stamp, _ in files:
        month = stamp.strftime("%Y-%m")
        # Keep the first recovery point of each job and one per month for a year.
        if group not in groups or (stamp >= now - timedelta(days=366) and month not in months):
            keep.add(name)
        groups.add(group)
        months.add(month)
    return {"count": len(files), "size_mb": round(sum(item[3] for item in files) / 1024 / 1024),
        "candidates": [{"name": name, "size": size} for name, _, stamp, size in files
                       if stamp < now - timedelta(days=30) and name not in keep], "deletion_enabled": False}


def collect(base=Path("/volume1/docker/family-workbench")):
    tasks = []
    cron = Path("/etc/crontab").read_text()
    for task_id, (mode, clocks, schedule) in TASKS.items():
        result = subprocess.run(["/usr/syno/bin/synoschedtask", "--get", f"id={task_id}"],
                                capture_output=True, text=True, timeout=10)
        description = result.stdout
        registered = [line.split() for line in cron.splitlines()
                      if re.search(r"--run id=" + str(task_id) + r"(?:\s|$)", line)]
        expected_minutes = {str(minute) for _, minute in clocks}
        expected_hours = {str(hour) for hour, _ in clocks}
        frequency_ok = (len(registered) == 1 and len(registered[0]) >= 7
                        and set(registered[0][0].split(",")) == expected_minutes
                        and set(registered[0][1].split(",")) == expected_hours
                        and registered[0][2:6] == ["*", "*", "*", "root"])
        enabled = ("State: [enabled]" in description and "Owner: [root]" in description
                   and f"Name: [family-workbench-macro-{mode}]" in description
                   and frequency_ok)
        path = base / "logs" / f"macro-{mode}.log"
        with path.open("rb") if path.exists() else open("/dev/null", "rb") as stream:
            stream.seek(max(0, path.stat().st_size - 200000) if path.exists() else 0)
            tail = stream.read().decode("utf-8", errors="replace")
        starts = re.findall(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+]08:00$", tail, re.M)
        proven = [stamp for stamp in starts if (datetime.fromisoformat(stamp).hour, datetime.fromisoformat(stamp).minute) in clocks]
        tasks.append({"id": task_id, "name": f"family-workbench-macro-{mode}", "enabled": enabled,
                      "schedule": schedule, "scheduled_trigger": proven[-1] if proven else "",
                      "result": "已观察到计划时刻启动：" + proven[-1] if proven else "已登记；等待计划时刻运行证据"})
    return {"tasks": tasks, "backups": backup_inventory(base / "backups")}


def ingest(payload, when):
    from .models import MacroOperationsSnapshot
    if not isinstance(payload, dict):
        raise ValueError("invalid macro host evidence")
    tasks = payload.get("tasks", [])
    if len(tasks) != 3 or {t.get("id") for t in tasks} != set(TASKS):
        raise ValueError("invalid macro task IDs")
    for task in tasks:
        mode = TASKS[task["id"]][0]
        if task.get("name") != "family-workbench-macro-" + mode or not isinstance(task.get("enabled"), bool):
            raise ValueError("invalid macro task evidence")
    backup = payload.get("backups", {})
    if backup.get("deletion_enabled") is not False:
        raise ValueError("backup deletion is not enabled")
    if any(not BACKUP_NAME.fullmatch(item.get("name", "")) for item in backup.get("candidates", [])):
        raise ValueError("unreviewed backup path")
    MacroOperationsSnapshot.objects.update_or_create(pk=1, defaults={"checked_at": when, "payload": payload})
