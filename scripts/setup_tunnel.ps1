# Put iRadar on iradar.net over HTTPS, via a NAMED Cloudflare tunnel.
#
# The quick tunnels used until now hand out a random trycloudflare.com address
# that changes every restart - useless for a team and impossible to bookmark. A
# named tunnel is bound to the domain, keeps the same address forever, and
# survives restarts. Cloudflare terminates TLS at its edge, so the team gets a
# real certificate without one ever existing on this machine, and nothing has to
# be exposed on the router.
#
# Run  .\setup_tunnel.ps1        to set it up (safe to re-run)
# Run  .\setup_tunnel.ps1 -Serve to just start serving with the existing setup
param(
    [string]$Domain   = "iradar.net",
    [string]$Name     = "iradar",
    [int]   $Port     = 5000,
    [switch]$Serve,
    [switch]$InstallService
)

$ErrorActionPreference = "Stop"
$here  = Split-Path -Parent $MyInvocation.MyCommand.Path
$cf    = Join-Path $here "cloudflared.exe"
$cfDir = Join-Path $env:USERPROFILE ".cloudflared"
$cert  = Join-Path $cfDir "cert.pem"
$conf  = Join-Path $cfDir "config.yml"

function Step($m) { Write-Host "`n== $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "   $m" -ForegroundColor Green }
function Warn($m) { Write-Host "   $m" -ForegroundColor Yellow }

if (-not (Test-Path $cf)) { throw "cloudflared.exe not found next to this script." }

# --- 1. the one step that needs a person -----------------------------------
if (-not (Test-Path $cert)) {
    Step "Cloudflare sign-in needed"
    Warn "A browser will open. Sign in and pick $Domain from the list."
    Warn "This authorises THIS machine to create tunnels on your account;"
    Warn "it writes a certificate to $cert and is the only interactive step."
    Write-Host ""
    Read-Host "Press Enter to open the browser"
    & $cf tunnel login
    if (-not (Test-Path $cert)) { throw "Sign-in did not complete - no cert.pem was written." }
}
Ok "Cloudflare account authorised."

# --- 2. the tunnel ----------------------------------------------------------
Step "Tunnel '$Name'"
$existing = (& $cf tunnel list) 2>&1 | Select-String -Pattern "\s$Name\s"
if ($existing) {
    Ok "Already exists."
} else {
    & $cf tunnel create $Name
    Ok "Created."
}

# the credentials file is named after the tunnel's UUID
$uuid = ((& $cf tunnel list) 2>&1 | Select-String -Pattern "\s$Name\s" |
         ForEach-Object { ($_ -split '\s+')[0] } | Select-Object -First 1)
if (-not $uuid) { throw "Could not read the tunnel id back from cloudflared." }
Ok "Tunnel id $uuid"

# --- 3. what the tunnel serves ---------------------------------------------
Step "Config"
@"
# Written by setup_tunnel.ps1 - iRadar
tunnel: $uuid
credentials-file: $(Join-Path $cfDir "$uuid.json")

ingress:
  # the dashboard
  - hostname: $Domain
    service: http://localhost:$Port
  - hostname: www.$Domain
    service: http://localhost:$Port
  # anything else that reaches this tunnel is not ours
  - service: http_status:404
"@ | Set-Content -Encoding utf8 $conf
Ok "Wrote $conf"

# --- 4. DNS ------------------------------------------------------------------
Step "DNS records"
foreach ($h in @($Domain, "www.$Domain")) {
    try {
        & $cf tunnel route dns $Name $h 2>&1 | Out-Null
        Ok "$h -> tunnel"
    } catch {
        Warn "$h : $($_.Exception.Message)"
    }
}

# --- 5. run it ---------------------------------------------------------------
if ($InstallService) {
    Step "Installing as a Windows service"
    Warn "Needs an elevated prompt. The tunnel then comes back by itself after"
    Warn "a reboot - the app still needs to be running for it to serve anything."
    & $cf --config $conf service install
    Ok "Installed."
} else {
    Step "Starting the tunnel"
    Warn "Leave this window open. Re-run with -InstallService to have Windows"
    Warn "keep it running across reboots instead."
    Write-Host ""
    Write-Host "   https://$Domain" -ForegroundColor Green
    Write-Host ""
    & $cf --config $conf tunnel run $Name
}
