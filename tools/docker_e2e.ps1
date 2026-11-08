# End to end inside the image, from Windows PowerShell: a device that serves its configuration over HTTP is backed up,
# reconfigured and backed up again (read-only root, no capabilities). Needs Docker Desktop.
# Usage: .\tools\docker_e2e.ps1 [-Image cfgtrack]
param([string]$Image = "cfgtrack")
$ErrorActionPreference = "Stop"

$id = Get-Random
$vol = "cfgtrack-e2e-$id"
$www = "cfgtrack-e2e-www-$id"
$net = "cfgtrack-e2e-net-$id"
$srv = "cfgtrack-e2e-srv-$id"

function Invoke-Cfg {
    $out = docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp --network $net -v "${vol}:/data" $Image @args
    if ($LASTEXITCODE -ne 0) { throw "cfgtrack $args failed (exit $LASTEXITCODE)" }
    $out | Out-String
}
function Assert-Contains($text, $needle) { if ($text -notmatch [regex]::Escape($needle)) { throw "expected '$needle' in: $text" } }
function Assert-Missing($text, $needle) { if ($text -match [regex]::Escape($needle)) { throw "did not expect '$needle' in: $text" } }

function Get-DeviceConfig($description, $n) {
@"
Building configuration...

Current configuration : 1$n bytes
!
! Last configuration change at 10:11:$n UTC Mon Nov 3 2026 by admin
!
hostname core
enable secret 9 `$9`$abcdefghijklmn
username admin privilege 15 secret 9 `$9`$zzzzzzzzzz
!
interface GigabitEthernet0/1
 description $description
 switchport mode access
!
interface GigabitEthernet0/2
 description printer
 shutdown
!
snmp-server community public123 RO
ntp clock-period 171798$n
line vty 0 4
 password 7 0822455D0A16
 login
!
end
"@
}
function Publish-Config($text) {
    $text | docker run --rm -i --user 0 -v "${www}:/www" --entrypoint sh $Image -c "cat > /www/core.cfg && chmod 644 /www/core.cfg"
    if ($LASTEXITCODE -ne 0) { throw "could not publish the configuration" }
}

try {
    docker network create $net | Out-Null
    docker volume create $www | Out-Null
    docker volume create $vol | Out-Null
    Publish-Config (Get-DeviceConfig "uplink" "10")
    docker run -d --name $srv --network $net -v "${www}:/www:ro" --entrypoint python $Image -m http.server 8000 --directory /www | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "could not start the test device" }
    Start-Sleep -Seconds 2

    $conf = @"
[store]
path = "backups"

[[device]]
name = "core"
profile = "cisco_ios"
fetch = "http"
url = "http://${srv}:8000/core.cfg"
"@
    $conf | docker run --rm -i -v "${vol}:/data" --entrypoint sh $Image -c "cat > /data/cfgtrack.toml"
    if ($LASTEXITCODE -ne 0) { throw "could not write the configuration" }

    Assert-Contains (Invoke-Cfg backup) "new"
    Assert-Contains (Invoke-Cfg backup) "0 changed, 0 failed"

    Publish-Config (Get-DeviceConfig "uplink-core" "47")
    Assert-Contains (Invoke-Cfg backup --diff) "+   description uplink-core"
    Assert-Contains (Invoke-Cfg log core) "core: +1 -1"
    $shown = Invoke-Cfg show core
    Assert-Contains $shown "<secret:"
    foreach ($s in @("public123", "0822455D0A16", "abcdefghijklmn")) { Assert-Missing $shown $s }
    Assert-Contains (Invoke-Cfg report --since 1d --format html) "uplink-core"
    Assert-Contains (Invoke-Cfg verify) "0 problem"

    docker rm -f $srv | Out-Null
    docker run --rm --read-only --cap-drop ALL --tmpfs /tmp --network $net -v "${vol}:/data" $Image backup | Out-Null
    if ($LASTEXITCODE -ne 1) { throw "expected exit code 1 when the device is gone, got $LASTEXITCODE" }
    Write-Host "docker end-to-end: OK"
}
finally {
    docker rm -f $srv 2>$null | Out-Null
    docker volume rm -f $vol 2>$null | Out-Null
    docker volume rm -f $www 2>$null | Out-Null
    docker network rm $net 2>$null | Out-Null
}
