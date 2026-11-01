#!/usr/bin/env bash
# Imports blackbox-dashboard.json into Grafana. Creates a Prometheus data source first when there is none.
#
#   ./import_dashboard.sh [grafana-url] [user]
#
# Defaults: http://10.20.20.171:3000 and admin. The password is asked for on the terminal (not shown, not part
# of any command line); it is handed to curl through a temporary file descriptor.
set -euo pipefail

GRAFANA="${1:-http://10.20.20.171:3000}"
USER_NAME="${2:-admin}"
DIR="$(cd "$(dirname "$0")" && pwd)"

read -rs -p "Passwort für ${USER_NAME} an ${GRAFANA}: " PW
echo

api() {
  curl -fsS -K <(printf 'user = "%s:%s"\n' "$USER_NAME" "$PW") -H 'Content-Type: application/json' "$@"
}

ds_uid="$(api "$GRAFANA/api/datasources" | python3 -c '
import json, sys
found = [d for d in json.load(sys.stdin) if d["type"] == "prometheus"]
print(found[0]["uid"] if found else "")')"

if [ -z "$ds_uid" ]; then
  echo "Keine Prometheus-Datenquelle vorhanden, lege \"Prometheus\" an (http://prometheus:9090)."
  ds_uid="$(api -X POST "$GRAFANA/api/datasources" -d '{"name":"Prometheus","type":"prometheus","url":"http://prometheus:9090","access":"proxy","isDefault":true}' |
    python3 -c 'import json, sys; print(json.load(sys.stdin)["datasource"]["uid"])')"
else
  echo "Verwende die vorhandene Prometheus-Datenquelle (${ds_uid})."
fi

payload="$(python3 - "$DIR/blackbox-dashboard.json" "$ds_uid" <<'EOF'
import json, sys
dashboard = json.load(open(sys.argv[1], encoding="utf-8"))
print(json.dumps({
    "dashboard": dashboard,
    "overwrite": True,
    "inputs": [{"name": "DS_PROMETHEUS", "type": "datasource", "pluginId": "prometheus", "value": sys.argv[2]}],
}))
EOF
)"

result="$(api -X POST "$GRAFANA/api/dashboards/import" -d "$payload")"
echo "$result" | python3 -c '
import json, sys
r = json.load(sys.stdin)
print("Importiert:", r.get("title"), "->", r.get("importedUrl"))'
echo "Öffne das Dashboard unter: ${GRAFANA}$(echo "$result" | python3 -c 'import json, sys; print(json.load(sys.stdin).get("importedUrl", ""))')"
