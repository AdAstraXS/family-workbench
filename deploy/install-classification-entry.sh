#!/bin/sh
# Fixed one-time installation authorized by the user. No arguments or passwords.
set -eu
umask 077
[ "$#" -eq 0 ] || { echo 'This installer takes no arguments.' >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo 'NAS administrator authentication is required.' >&2; exit 1; }

original=/usr/local/sbin/family-workbench-deploy
candidate=/volume1/homes/DX/family-workbench-deploy.classification-bootstrap
backup=/usr/local/sbin/family-workbench-deploy.before-classification-20261006
receipt=/volume1/docker/family-workbench/backups/classification-entry-install-20261006.receipt
old_sha=27b3d8008df8a8f9236c37e0c59d695d49a54563c4c563d51868383ddb520c03
new_sha=0b0025d1bf58f603e33e83388c21f2a355146c64c073cfdf84ec691b6bc7ef5c

hash() { sha256sum "$1" | awk '{print $1}'; }
die() { printf 'STOP: %s\n' "$*" >&2; exit 1; }
[ -f "$original" ] && [ ! -L "$original" ] || die 'Original wrapper is not a regular file.'
[ "$(stat -c '%u:%a' "$original")" = 0:755 ] || die 'Original wrapper permissions changed.'
actual_sha="$(hash "$original")"
if [ "$actual_sha" = "$new_sha" ]; then
    [ -f "$backup" ] && [ ! -L "$backup" ] && [ "$(hash "$backup")" = "$old_sha" ] || die 'Existing installation lacks verified rollback copy.'
    "$original" help
    "$original" status
    printf 'Already installed and verified.\n'
    exit 0
fi
[ "$actual_sha" = "$old_sha" ] || die 'Original wrapper changed; refusing to overwrite.'
[ -f "$candidate" ] && [ ! -L "$candidate" ] || die 'Candidate is not a regular file.'
[ "$(hash "$candidate")" = "$new_sha" ] || die 'Candidate SHA-256 mismatch.'
stage="$(mktemp /usr/local/sbin/.family-workbench-classification.XXXXXX)"
switched=0
complete=0
cleanup() {
    result=$?
    trap - EXIT HUP INT TERM
    if [ "$switched" = 1 ] && [ "$complete" != 1 ]; then
        if [ -f "$backup" ] && [ ! -L "$backup" ] && [ "$(hash "$backup")" = "$old_sha" ]; then
            cp -p "$backup" "$original"
            printf 'Installation verification failed; original wrapper restored.\n' >&2
        else
            printf 'Rollback copy changed; stop and inspect original wrapper.\n' >&2
        fi
    fi
    case "$stage" in /usr/local/sbin/.family-workbench-classification.*) rm -f "$stage" ;; esac
    exit "$result"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
install -o root -g root -m 0755 "$candidate" "$stage"
[ "$(hash "$stage")" = "$new_sha" ] || die 'Staged candidate changed; nothing installed.'
sh -n "$stage"
if [ -e "$backup" ] || [ -L "$backup" ]; then
    [ -f "$backup" ] && [ ! -L "$backup" ] && [ "$(hash "$backup")" = "$old_sha" ] || die 'Rollback path already contains a different file.'
else
    cp -p "$original" "$backup"
fi
[ "$(hash "$backup")" = "$old_sha" ] || die 'Rollback verification failed.'
[ "$(hash "$original")" = "$old_sha" ] || die 'Original changed during preparation.'
switched=1
mv "$stage" "$original"
[ "$(hash "$original")" = "$new_sha" ] || die 'Installed SHA-256 mismatch.'
[ "$(stat -c '%u:%a' "$original")" = 0:755 ] || die 'Installed ownership or permissions incorrect.'
"$original" help
"$original" status
if "$original" unsupported-command >/dev/null 2>&1; then
    die 'Wrapper accepted an unsupported command.'
fi
[ ! -L "$receipt" ] || die 'Receipt cannot be a symbolic link.'
{
    date -Iseconds
    printf 'ORIGINAL_SHA256=%s\nINSTALLED_SHA256=%s\nROLLBACK_FILE=%s\n' "$old_sha" "$new_sha" "$backup"
    printf 'SSH_AND_SUDOERS=unchanged\nFINANCIAL_DATABASE=unchanged\n'
} > "$receipt"
chmod 600 "$receipt"
complete=1
printf 'INSTALLATION_VERIFIED\nROLLBACK_FILE=%s\nRECEIPT=%s\n' "$backup" "$receipt"
