#!/bin/bash
# pat-sig-mqtt-watchdog — restart pat-sig if stuck in the MQTT reconnect loop.
# 2026-07-10 · defense-in-depth beside the mqtt_service _reconnect_loop patch.
set -u
WINDOW=${WINDOW:-120}; MIN_RECON=${MIN_RECON:-10}
CONNECTED_MAX_AGE=${CONNECTED_MAX_AGE:-100}; COOLDOWN=${COOLDOWN:-600}
DRY_RUN=${DRY_RUN:-0}; STATE=/run/pat-sig-mqtt-watchdog.last
now=$(date +%s)
log=$(journalctl -u pat-sig --since "-${WINDOW}s" -o short-unix --no-pager 2>/dev/null)
recon=$(printf '%s\n' "$log" | grep -c "MQTT reconnecting in 5 seconds")
last_conn=$(printf '%s\n' "$log" | grep "MQTT connected successfully" | tail -1 | awk '{print int($1)}')
[ -z "${last_conn:-}" ] && last_conn=0
if [ "$last_conn" -gt 0 ]; then conn_age=$(( now - last_conn )); else conn_age=999999; fi
stuck=0
[ "$recon" -ge "$MIN_RECON" ] && [ "$conn_age" -gt "$CONNECTED_MAX_AGE" ] && stuck=1
msg="pat-sig-mqtt-watchdog: recon=${recon}/${WINDOW}s conn_age=${conn_age}s"
if [ "$stuck" -eq 1 ]; then
  last_restart=0; [ -f "$STATE" ] && last_restart=$(cat "$STATE" 2>/dev/null || echo 0)
  if [ $(( now - last_restart )) -lt "$COOLDOWN" ]; then
    logger -t pat-sig-watchdog "$msg STUCK — cooldown, skip"; echo "$msg STUCK — cooldown skip"; exit 0
  fi
  if [ "$DRY_RUN" = "1" ]; then echo "$msg STUCK — WOULD RESTART pat-sig (dry-run)"; exit 0; fi
  echo "$now" > "$STATE"
  logger -t pat-sig-watchdog "$msg STUCK — restarting pat-sig"
  systemctl restart pat-sig; echo "$msg STUCK — restarted pat-sig"
else
  [ "$DRY_RUN" = "1" ] && echo "$msg healthy — no-op (dry-run)"; exit 0
fi
