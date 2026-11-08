#!/usr/bin/env bash
# End to end inside the image (read-only root, no capabilities): a device that serves its configuration over HTTP is
# backed up, reconfigured, backed up again, and the history, the diff, the HTML report and the checks are produced
# from the same volume. Needs Docker. Usage: tools/docker_e2e.sh [image]
set -euo pipefail
IMAGE="${1:-cfgtrack}"
ID="$$"
VOL="cfgtrack-e2e-$ID"
WWW="cfgtrack-e2e-www-$ID"
NET="cfgtrack-e2e-net-$ID"
SRV="cfgtrack-e2e-srv-$ID"
TMP="$(mktemp -d)"
trap 'docker rm -f "$SRV" >/dev/null 2>&1 || true; docker volume rm -f "$VOL" "$WWW" >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true; rm -rf "$TMP"' EXIT

[ "$(docker run --rm --entrypoint id "$IMAGE" -u)" != 0 ]   # not root
docker run --rm --entrypoint git "$IMAGE" --version >/dev/null

device_config() {   # $1 = description of the uplink, $2 = a number that changes on every run (noise)
cat <<CFG
Building configuration...

Current configuration : 1$2 bytes
!
! Last configuration change at 10:11:$2 UTC Mon Nov 3 2026 by admin
!
hostname core
enable secret 9 \$9\$abcdefghijklmn
username admin privilege 15 secret 9 \$9\$zzzzzzzzzz
!
interface GigabitEthernet0/1
 description $1
 switchport mode access
!
interface GigabitEthernet0/2
 description printer
 shutdown
!
snmp-server community public123 RO
ntp clock-period 171798$2
line vty 0 4
 password 7 0822455D0A16
 login
!
end
CFG
}

publish() {   # put a configuration where the web server serves it from
  chmod 644 "$TMP/core.cfg"
  docker run --rm --user 0 -v "$WWW":/www -v "$TMP/core.cfg":/in.cfg:ro --entrypoint sh "$IMAGE" -c 'cat /in.cfg > /www/core.cfg && chmod 644 /www/core.cfg'
}

docker network create "$NET" >/dev/null
docker volume create "$WWW" >/dev/null
docker volume create "$VOL" >/dev/null
device_config uplink 10 > "$TMP/core.cfg"
publish
docker run -d --name "$SRV" --network "$NET" -v "$WWW":/www:ro --entrypoint python "$IMAGE" -m http.server 8000 --directory /www >/dev/null
sleep 2

cat > "$TMP/cfgtrack.toml" <<TOML
[store]
path = "backups"

[[device]]
name = "core"
profile = "cisco_ios"
fetch = "http"
url = "http://$SRV:8000/core.cfg"
TOML
chmod 644 "$TMP/cfgtrack.toml"
docker run --rm -v "$VOL":/data -v "$TMP/cfgtrack.toml":/in.toml:ro --entrypoint sh "$IMAGE" -c 'cat /in.toml > /data/cfgtrack.toml'

RUN=(docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp --network "$NET" -v "$VOL":/data "$IMAGE")
"${RUN[@]}" backup | tee /dev/stderr | grep -q 'core  *new'
"${RUN[@]}" backup | tee /dev/stderr | grep -q '0 changed, 0 failed'           # only the noise differs: nothing is stored

device_config uplink-core 47 > "$TMP/core.cfg"
publish
"${RUN[@]}" backup --diff | tee /dev/stderr | grep -q '+   description uplink-core'
"${RUN[@]}" log core | tee /dev/stderr | grep -q 'core: +1 -1'
"${RUN[@]}" show core | tee /dev/stderr > "$TMP/shown.txt"
grep -q '<secret:' "$TMP/shown.txt"
! grep -q -e public123 -e 0822455D0A16 -e abcdefghijklmn "$TMP/shown.txt"      # no password in what was stored
"${RUN[@]}" report --since 1d --format html | grep -q 'uplink-core'
"${RUN[@]}" status | tee /dev/stderr | grep -q ' ok '
"${RUN[@]}" verify | tee /dev/stderr | grep -q '0 problem'

# a device that does not answer is a failure, with exit code 1, and the stored configuration is left alone
docker rm -f "$SRV" >/dev/null
if "${RUN[@]}" backup >/dev/null; then echo "expected exit 1 when the device is gone" >&2; exit 1; fi
"${RUN[@]}" log core | grep -c 'core:' | grep -q '^2$'                            # still two commits
echo "docker end-to-end: OK"
