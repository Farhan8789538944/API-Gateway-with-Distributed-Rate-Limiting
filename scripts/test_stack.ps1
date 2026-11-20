<#
.SYNOPSIS
  Manage the ISOLATED test stack (Compose project "apigw-test", its own volumes).

.EXAMPLE
  .\scripts\test_stack.ps1 up       # build + start + wait until healthy
  .\scripts\test_stack.ps1 status
  .\scripts\test_stack.ps1 logs gateway-1
  .\scripts\test_stack.ps1 down     # stop and remove containers (keeps volumes)
  .\scripts\test_stack.ps1 reset    # DESTRUCTIVE for the test project only: also deletes its volumes
#>
param(
    [Parameter(Mandatory = $true)][ValidateSet("up", "down", "reset", "status", "logs")] [string]$Action,
    [string]$Service = "",
    [string]$Project = "apigw-test"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    if (-not (Test-Path ".env")) { throw "Missing .env - run: Copy-Item .env.example .env" }
    $compose = @("compose", "-p", $Project, "-f", "compose.yaml", "-f", "compose.test.yaml")
    switch ($Action) {
        "up"     { docker @compose up -d --build --wait --wait-timeout 180 }
        "down"   { docker @compose down --remove-orphans }
        "reset"  { docker @compose down --remove-orphans --volumes }
        "status" { docker @compose ps }
        "logs"   { if ($Service) { docker @compose logs --tail 200 $Service } else { docker @compose logs --tail 100 } }
    }
    if ($LASTEXITCODE -ne 0) { throw "docker compose $Action failed with exit code $LASTEXITCODE" }
}
finally { Pop-Location }
