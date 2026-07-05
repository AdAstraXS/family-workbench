#!/usr/bin/env bash
set -euo pipefail

runtime=/opt/futu
state=/var/lib/futu
config=/run/futu/FutuOpenD.xml

if [[ ! -x "$runtime/FutuOpenD" || ! -f "$runtime/AppData.dat" ]]; then
  echo "OpenD runtime is incomplete: FutuOpenD and AppData.dat are required." >&2
  exit 1
fi

if [[ ! "${FUTU_LOGIN_ACCOUNT:-}" =~ ^[0-9]+$ ]]; then
  echo "FUTU_LOGIN_ACCOUNT must be a numeric Futubull ID." >&2
  exit 1
fi

if [[ ! "${FUTU_LOGIN_PWD_MD5:-}" =~ ^[0-9a-fA-F]{32}$ ]]; then
  echo "FUTU_LOGIN_PWD_MD5 must be a 32-character hexadecimal MD5 value." >&2
  exit 1
fi

mkdir -p /run/futu "$state/logs"
chmod 0700 "$state"

cat > "$config" <<EOF
<futu_opend>
  <ip>0.0.0.0</ip>
  <api_port>11111</api_port>
  <login_account>${FUTU_LOGIN_ACCOUNT}</login_account>
  <login_pwd_md5>${FUTU_LOGIN_PWD_MD5,,}</login_pwd_md5>
  <lang>chs</lang>
  <log_level>info</log_level>
  <log_path>${state}/logs</log_path>
  <push_proto_type>0</push_proto_type>
  <telnet_ip>127.0.0.1</telnet_ip>
  <telnet_port>22222</telnet_port>
  <price_reminder_push>0</price_reminder_push>
  <auto_hold_quote_right>0</auto_hold_quote_right>
  <future_trade_api_time_zone>UTC+8</future_trade_api_time_zone>
</futu_opend>
EOF
chmod 0600 "$config"

exec "$runtime/FutuOpenD" -cfg_file="$config" -console=1
