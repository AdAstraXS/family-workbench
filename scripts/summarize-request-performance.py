import sys, re, json, math
from collections import defaultdict

pattern = re.compile(r'request route=(\S+) method=(\S+) status=(\d+) elapsed_ms=([\d.]+) queries=(\d+) db_ms=([\d.]+) bytes=(-?\d+)')
groups = defaultdict(list)
for line in sys.stdin:
    match = pattern.search(line)
    if match:
        route, method, status, elapsed, queries, database, size = match.groups()
        groups[(route, method)].append((float(elapsed), int(queries), float(database), int(size), int(status)))
def percentile(values, fraction):
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * fraction) - 1)]
for (route, method), rows in sorted(groups.items()):
    print(json.dumps({'route': route, 'method': method, 'requests': len(rows),
        'p50_ms': percentile([r[0] for r in rows], .5), 'p95_ms': percentile([r[0] for r in rows], .95),
        'max_queries': max(r[1] for r in rows), 'p95_db_ms': percentile([r[2] for r in rows], .95),
        'max_bytes': max(r[3] for r in rows), 'errors': sum(r[4] >= 500 for r in rows)}, ensure_ascii=False))
