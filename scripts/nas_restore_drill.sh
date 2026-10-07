#!/bin/sh
# One-time root operation reviewed and run by the NAS administrator.
# Fixed source; never restores into production or grants additional sudo rights.
set -eu
set +x
umask 077
[ "$#" -eq 0 ] || { echo 'This script accepts no arguments.' >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || { echo 'Run this reviewed script as the NAS administrator.' >&2; exit 2; }
ROOT=/volume1/docker/family-workbench
DOCKER=/volume1/@appstore/ContainerManager/usr/bin/docker
WRAPPER=/usr/local/sbin/family-workbench-deploy
for path in "$ROOT" "$ROOT/backups" "$ROOT/knowledge_files" "$ROOT/media" "$ROOT/app"; do
    [ -d "$path" ] && [ ! -L "$path" ] || { echo 'Unexpected source path.' >&2; exit 2; }
done
[ -f "$ROOT/app/monitoring/management/commands/verify_restored_workbench.py" ] || {
    echo 'Deploy the restore verification command first.' >&2; exit 2;
}
cd "$ROOT"
dc() { "$DOCKER" compose --project-directory "$ROOT" -f "$ROOT/docker-compose.yml" "$@"; }
identifier=$(cat /proc/sys/kernel/random/uuid | tr -d '-')
label="drill-$(date -u +%Y%m%dT%H%M%S)-${identifier}"
OUT="$ROOT/backups/$label"
NAME="workbench-restore-$identifier"
mkdir -m 700 "$OUT"
started=0
cleanup() {
    unset KNOWLEDGE_TOKEN_ENCRYPTION_KEY DJANGO_SECRET_KEY
    if [ "$started" -eq 1 ]; then "$DOCKER" stop --time 10 "$NAME" >/dev/null || true; fi
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM
baseline() {
    dc exec -T web python - <<'PY'
import os, json
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()
from django.apps import apps
from monitoring.restore_verify import BASELINE_MODELS
data = {label: apps.get_model(label).objects.count() for label in BASELINE_MODELS}
latest = apps.get_model('portfolio.PortfolioSnapshot').objects.order_by('-snapshot_date').values_list('snapshot_date', flat=True).first()
data['latest_snapshot_date'] = str(latest) if latest else None
print(json.dumps(data, sort_keys=True))
PY
}
baseline > "$OUT/baseline.json"
"$WRAPPER" backup-db "$label"
DUMP="$ROOT/backups/family-workbench-$label.dump"
[ -f "$DUMP" ] && [ ! -L "$DUMP" ] || { echo 'Verified backup was not found.' >&2; exit 1; }
digest=$(sha256sum "$DUMP" | cut -d ' ' -f 1)
echo 'Copying knowledge files and media into the private recovery point.'
cp -a "$ROOT/knowledge_files" "$OUT/knowledge"
cp -a "$ROOT/media" "$OUT/media"
baseline > "$OUT/baseline-after.json"
cmp -s "$OUT/baseline.json" "$OUT/baseline-after.json" || {
    echo 'Production baseline changed during backup; retained copies, stopped verification.' >&2; exit 1;
}
db_image=$("$DOCKER" inspect --format '{{.Image}}' family_finance_db)
web_image=$("$DOCKER" inspect --format '{{.Image}}' family_finance_web)
# Values are kept only in the process environment, never printed or saved.
KNOWLEDGE_TOKEN_ENCRYPTION_KEY=$(dc exec -T web python -c 'import os; os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings"); from django.conf import settings; print(settings.KNOWLEDGE_TOKEN_ENCRYPTION_KEY)' 2>/dev/null)
DJANGO_SECRET_KEY="$identifier$identifier"
export KNOWLEDGE_TOKEN_ENCRYPTION_KEY DJANGO_SECRET_KEY
"$DOCKER" run --rm -d --name "$NAME" --network none \
    -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=restore_drill_workbench "$db_image" >/dev/null
started=1
attempt=0
until "$DOCKER" exec "$NAME" pg_isready -h 127.0.0.1 -U postgres -d restore_drill_workbench >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    [ "$attempt" -lt 60 ] || { echo 'Isolated database did not become ready.' >&2; exit 1; }
    sleep 1
done
"$DOCKER" exec -i "$NAME" pg_restore --no-owner --no-acl --exit-on-error \
    -U postgres -d restore_drill_workbench < "$DUMP" > "$OUT/restore.log" 2>&1 || {
    echo 'Restore failed; see the private restore.log on the NAS.' >&2; exit 1;
}
echo 'Restored isolated database; checking copied files and encrypted records.'
"$DOCKER" run --rm --network "container:$NAME" \
    -e DJANGO_DEBUG=False -e DJANGO_SECRET_KEY -e KNOWLEDGE_TOKEN_ENCRYPTION_KEY \
    -e DATABASE_URL=postgresql://postgres@127.0.0.1:5432/restore_drill_workbench \
    -e RESTORE_DRILL_ISOLATED=1 -e "RESTORE_DRILL_RESTORED=$digest" \
    -v "$ROOT/app:/app:ro" -v "$OUT:/drill" \
    -v "$OUT/baseline.json:/drill/baseline.json:ro" \
    -v "$OUT/knowledge:/drill/knowledge:ro" -v "$OUT/media:/drill/media:ro" -w /app "$web_image" \
    python manage.py verify_restored_workbench --baseline /drill/baseline.json \
    --backup-sha256 "$digest" --output /drill/restore-report.json \
    --knowledge-root /drill/knowledge --media-root /drill/media
echo "Report: $OUT/restore-report.json"
echo 'Private backup and file copies retained; isolated container will now stop.'
