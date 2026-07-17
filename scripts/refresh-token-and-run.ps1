Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

$envPath = Join-Path $projectRoot '.env'
if (-not (Test-Path $envPath)) {
    throw '.env file not found in project root.'
}

# Parse key=value lines from .env (ignores comments/blank lines).
$map = @{}
Get-Content $envPath | ForEach-Object {
    if ($_ -match '^\s*#' -or $_ -match '^\s*$') { return }
    $parts = $_ -split '=', 2
    if ($parts.Count -eq 2) {
        $key = $parts[0].Trim()
        $value = $parts[1].Trim().Trim('"').Trim("'")
        $map[$key] = $value
    }
}

if (-not $map.ContainsKey('AAD_TENANT_ID') -or [string]::IsNullOrWhiteSpace($map['AAD_TENANT_ID'])) {
    throw 'AAD_TENANT_ID is missing in .env'
}
if (-not $map.ContainsKey('AAD_SCOPE') -or [string]::IsNullOrWhiteSpace($map['AAD_SCOPE'])) {
    throw 'AAD_SCOPE is missing in .env'
}

$tenantId = $map['AAD_TENANT_ID']
$scope = $map['AAD_SCOPE']
$resource = $scope
if ($scope -like '*/.default') {
    $resource = $scope.Substring(0, $scope.Length - '/.default'.Length)
}

Write-Host "[1/4] Refresh delegated token from Azure CLI..."
$token = az account get-access-token --tenant $tenantId --resource $resource --query accessToken -o tsv
if ([string]::IsNullOrWhiteSpace($token)) {
    throw 'Failed to obtain access token from Azure CLI. Run: az login --tenant <tenant-id>'
}

Write-Host "[2/4] Update .env token + mode..."
$updated = Get-Content $envPath | ForEach-Object {
    if ($_ -match '^\s*COPILOT_STUDIO_TOKEN_MODE=') {
        'COPILOT_STUDIO_TOKEN_MODE=raw'
    }
    elseif ($_ -match '^\s*COPILOT_STUDIO_BEARER_TOKEN=') {
        'COPILOT_STUDIO_BEARER_TOKEN=' + $token
    }
    else {
        $_
    }
}
$updated | Set-Content $envPath

Write-Host "[3/4] Stop old listener on port 3978 (if exists)..."
$conn = Get-NetTCPConnection -LocalPort 3978 -State Listen -ErrorAction SilentlyContinue
if ($null -ne $conn) {
    $pids = $conn | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($procId in $pids) {
        try {
            Stop-Process -Id $procId -Force -ErrorAction Stop
        }
        catch {
            Write-Warning "Could not stop PID ${procId}: $($_.Exception.Message)"
        }
    }
}

Write-Host "[4/4] Start app..."
py -3 app.py
