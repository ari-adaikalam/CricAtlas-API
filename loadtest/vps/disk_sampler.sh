#!/bin/bash
# Record the disk budget lines every 10 minutes into disk.csv, from the disk scan's metrics file, for the validation report. Read only.
#   disk_sampler.sh OUT_CSV [PROM_FILE]
out="${1:?usage: disk_sampler.sh OUT_CSV [PROM_FILE]}"; prom="${2:-/srv/cricket/textfile/cricket_disk.prom}"
components="docker_images docker_other api_data scratch ledger ledger_backups prometheus grafana journal caddy_logs var_log tmp_staging"
[ -e "$out" ] || echo "time_utc,root_free_gb,$(echo $components | tr ' ' ',')" > "$out"
while true; do
  line="$(date -u +%FT%TZ),$(df -BG --output=avail / | tail -n1 | tr -dc 0-9)"
  for c in $components; do
    v="$(sed -n "s/^cricket_disk_component_bytes{component=\"$c\"} //p" "$prom" 2>/dev/null | head -n1)"
    line="$line,${v:-}"
  done
  echo "$line" >> "$out"
  sleep 600
done
