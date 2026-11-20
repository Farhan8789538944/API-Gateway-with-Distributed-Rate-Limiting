<#
.SYNOPSIS
  Smoke benchmark of all workloads against the ISOLATED test stack (apigw-test).

.DESCRIPTION
  Requires: .\scripts\test_stack.ps1 up
  Locust runs in a container on the test stack's network (profile "loadtest"),
  so the direct-backend baseline needs no published ports. For every scenario
  a warm-up run (discarded) precedes the measured run. Raw Locust CSVs, the
  outcome summary JSON (successful latencies kept separately) and a
  `docker stats` snapshot taken mid-run are written to
  load_tests\results\<run-id>\. Then summarize with:
      .\.venv\Scripts\python.exe scripts\summarize_benchmarks.py load_tests\results\<run-id>

.EXAMPLE
  .\scripts\run_benchmarks.ps1                      # 20 users, 10 s warm-up, 30 s measured
  .\scripts\run_benchmarks.ps1 -Users 50 -Duration 60
#>
param(
    [int]$Users = 20,
    [int]$Duration = 30,
    [int]$Warmup = 10,
    [string]$Project = "apigw-test",
    [string[]]$Only = @()
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $compose = @("compose", "--profile", "loadtest", "-p", $Project, "-f", "compose.yaml", "-f", "compose.test.yaml")
    $runId = Get-Date -Format "yyyyMMdd-HHmmss"
    $outDir = Join-Path "load_tests/results" $runId
    New-Item -ItemType Directory -Force $outDir | Out-Null

    function Invoke-Admin([string[]]$AdminArgs) {
        $out = docker @compose run --rm --no-deps admin @AdminArgs 2>$null
        if ($LASTEXITCODE -ne 0) { throw "admin $($AdminArgs -join ' ') failed" }
        return $out
    }
    function New-BenchKey([string]$Client, [string]$Policy) {
        Invoke-Admin @("create-client", "--name", $Client, "--policy", $Policy) | Out-Null
        $out = Invoke-Admin @("issue-key", "--client", $Client, "--label", "benchmark")
        return (($out | Select-String '^api_key:').Line -split '\s+')[-1]
    }

    $suffix = $runId.Replace("-", "")
    $unlimitedKey = New-BenchKey "bench-unl-$suffix" "benchmark-unlimited"
    Invoke-Admin @("create-policy", "--name", "bench-quota-$suffix", "--algorithm", "token_bucket",
                   "--capacity", "50", "--refill-tokens", "50", "--refill-period-ms", "1000") | Out-Null
    $quotaKey = New-BenchKey "bench-quota-$suffix" "bench-quota-$suffix"
    Write-Host "benchmark keys issued (not printed)"

    function Set-ProductDelay([int]$DelayMs) {
        foreach ($port in 18081, 18082) {
            if ($DelayMs -gt 0) {
                Invoke-RestMethod -Method Put -Uri "http://127.0.0.1:$port/_dev/faults" -ContentType "application/json" -Body (@{ delay_ms = $DelayMs } | ConvertTo-Json) | Out-Null
            } else {
                Invoke-RestMethod -Method Delete -Uri "http://127.0.0.1:$port/_dev/faults" | Out-Null
            }
        }
    }

    function Invoke-Locust([string]$Name, [string]$TargetHost, [string]$UserClass, [string]$Key,
                           [string]$Wait, [int]$N, [int]$Seconds, [bool]$Measured) {
        $results = if ($Measured) { "/mnt/locust/results/$runId/$Name.json" } else { "" }
        $locustArgs = @("-f", "locustfile.py", $UserClass, "--headless", "-u", "$N", "-r", "$N",
                        "-t", "${Seconds}s", "--host", $TargetHost, "--only-summary", "--loglevel", "WARNING")
        if ($Measured) { $locustArgs += @("--csv", "results/$runId/$Name") }
        docker @compose run --rm --no-deps -e "API_KEY=$Key" -e "WAIT_MODE=$Wait" -e "RESULTS_FILE=$results" `
            locust @locustArgs 2>&1 | Out-File -Encoding utf8 (Join-Path $outDir "$Name.$(if ($Measured) {'measured'} else {'warmup'}).log")
    }

    $scenarios = @(
        @{ Name = "1-direct-baseline";      Host = "http://product-1:8000"; Class = "ProductReadUser"; Key = "";            Wait = "none"; Users = $Users },
        @{ Name = "2-one-gateway";          Host = "http://gateway-1:8000"; Class = "ProductReadUser"; Key = $unlimitedKey; Wait = "none"; Users = $Users },
        @{ Name = "3-two-gateways-reads";   Host = "http://nginx:80";       Class = "ProductReadUser"; Key = $unlimitedKey; Wait = "none"; Users = $Users },
        @{ Name = "4-product-order-mix";    Host = "http://nginx:80";       Class = "MixedUser";       Key = $unlimitedKey; Wait = "none"; Users = $Users },
        @{ Name = "5-quota-exhaustion";     Host = "http://nginx:80";       Class = "ProductReadUser"; Key = $quotaKey;     Wait = "throughput:10"; Users = $Users },
        @{ Name = "6-slow-backend-overload"; Host = "http://gateway-1:8000"; Class = "ProductReadUser"; Key = $unlimitedKey; Wait = "throughput:10"; Users = 80; DelayMs = 200 }
    )
    foreach ($s in $scenarios) {
        if ($Only.Count -gt 0 -and -not ($Only | Where-Object { $s.Name -like "*$_*" })) { continue }
        Write-Host "== $($s.Name): $($s.Users) users, $($s.Class) -> $($s.Host), wait=$($s.Wait)"
        if ($s.DelayMs) { Set-ProductDelay $s.DelayMs }
        try {
            Invoke-Locust $s.Name $s.Host $s.Class $s.Key $s.Wait $s.Users $Warmup $false
            $stats = Start-Job -ScriptBlock {
                param($delay, $file)
                Start-Sleep -Seconds $delay
                docker stats --no-stream --format "{{.Name}}`t{{.CPUPerc}}`t{{.MemUsage}}" | Out-File -Encoding utf8 $file
            } -ArgumentList ([int]($Duration / 2)), (Join-Path (Resolve-Path $outDir) "$($s.Name).docker-stats.tsv")
            Invoke-Locust $s.Name $s.Host $s.Class $s.Key $s.Wait $s.Users $Duration $true
            $stats | Wait-Job | Remove-Job
        }
        finally { if ($s.DelayMs) { Set-ProductDelay 0 } }
    }

    $meta = [ordered]@{
        run_id = $runId; users_default = $Users; warmup_s = $Warmup; measured_s = $Duration
        docker = (docker info --format '{{.NCPU}} CPUs, {{.MemTotal}} bytes memory, {{.OperatingSystem}}, engine {{.ServerVersion}}')
        host_cpu = (Get-CimInstance Win32_Processor | Select-Object -First 1).Name
        host_memory_gb = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
    }
    $meta | ConvertTo-Json | Out-File -Encoding utf8 (Join-Path $outDir "meta.json")
    Write-Host "raw results: $outDir"
}
finally { Pop-Location }
