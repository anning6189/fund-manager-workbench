[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$SourceId,
    [Parameter(Mandatory=$true)][string]$Uri,
    [ValidateSet('Head','Get')][string]$Method = 'Head',
    [string]$RegistryPath
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($RegistryPath)) {
    $RegistryPath = Join-Path $projectRoot 'specs\connectors\source-registry.v1.json'
}
$registry = Get-Content -Raw -Encoding UTF8 $RegistryPath | ConvertFrom-Json
$sourceMatches = @($registry.sources | Where-Object source_id -eq $SourceId)
if ($sourceMatches.Count -ne 1) { throw "SourceId must resolve exactly once: $SourceId" }
$source = $sourceMatches[0]

function Test-IsNonPublicAddress([Net.IPAddress]$Address) {
    if ([Net.IPAddress]::IsLoopback($Address)) { return $true }
    if ($Address.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork) {
        $bytes = $Address.GetAddressBytes()
        if ($bytes[0] -in @(0,10,127)) { return $true }
        if ($bytes[0] -eq 169 -and $bytes[1] -eq 254) { return $true }
        if ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) { return $true }
        if ($bytes[0] -eq 192 -and $bytes[1] -eq 168) { return $true }
        if ($bytes[0] -eq 100 -and $bytes[1] -ge 64 -and $bytes[1] -le 127) { return $true }
        if ($bytes[0] -ge 224) { return $true }
        return $false
    }
    if ($Address.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetworkV6) {
        if ($Address.IsIPv6LinkLocal -or $Address.IsIPv6Multicast -or $Address.IsIPv6SiteLocal) { return $true }
        $bytes = $Address.GetAddressBytes()
        if (($bytes[0] -band 0xFE) -eq 0xFC) { return $true }
        if ($Address.IsIPv4MappedToIPv6) { return (Test-IsNonPublicAddress $Address.MapToIPv4()) }
    }
    return $false
}

function Assert-AllowedPublicUri([Uri]$TargetUri) {
    if ($TargetUri.Scheme -ne 'https') { throw 'Only HTTPS endpoints are allowed.' }
    if (@($source.allowed_hosts) -notcontains $TargetUri.DnsSafeHost) { throw "Host not allowlisted for ${SourceId}: $($TargetUri.DnsSafeHost)" }
    if ($TargetUri.IsLoopback -or $TargetUri.DnsSafeHost -in @('localhost','127.0.0.1','::1')) { throw 'Loopback targets are forbidden.' }
    $resolved = @([Net.Dns]::GetHostAddresses($TargetUri.DnsSafeHost))
    if ($resolved.Count -eq 0) { throw "DNS returned no address for $($TargetUri.DnsSafeHost)." }
    foreach ($address in $resolved) {
        if (Test-IsNonPublicAddress $address) { throw "Non-public target address is forbidden: $address" }
    }
}

Add-Type -AssemblyName System.Net.Http
$handler = New-Object Net.Http.HttpClientHandler
$handler.AllowAutoRedirect = $false
$client = New-Object Net.Http.HttpClient($handler)
$client.Timeout = [TimeSpan]::FromSeconds(20)
$response = $null
$stopwatch = [Diagnostics.Stopwatch]::StartNew()
try {
    $current = [Uri]$Uri
    for ($redirects = 0; $redirects -le 3; $redirects++) {
        Assert-AllowedPublicUri $current
        $requestMethod = New-Object Net.Http.HttpMethod($Method.ToUpperInvariant())
        $request = New-Object Net.Http.HttpRequestMessage($requestMethod, $current)
        try {
            $response = $client.SendAsync($request, [Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
        } finally {
            $request.Dispose()
        }
        $statusCode = [int]$response.StatusCode
        if ($statusCode -ge 300 -and $statusCode -lt 400) {
            if ($redirects -eq 3) { throw 'Redirect limit exceeded.' }
            $location = $response.Headers.Location
            if ($null -eq $location) { throw 'Redirect response lacks a Location header.' }
            $next = if ($location.IsAbsoluteUri) { $location } else { New-Object Uri($current, $location) }
            $response.Dispose()
            $response = $null
            $current = $next
            continue
        }
        if (-not $response.IsSuccessStatusCode) { throw "HTTP $statusCode $($response.ReasonPhrase)" }
        $stopwatch.Stop()
        [ordered]@{
            source_id = $SourceId
            uri = ([Uri]$Uri).AbsoluteUri
            status = 'success'
            status_code = $statusCode
            method = $Method.ToUpperInvariant()
            final_uri = $current.AbsoluteUri
            redirect_count = $redirects
            latency_ms = $stopwatch.ElapsedMilliseconds
            tested_at = [DateTimeOffset]::Now.ToString('o')
        } | ConvertTo-Json
        exit 0
    }
} catch {
    $stopwatch.Stop()
    [ordered]@{
        source_id = $SourceId
        uri = ([Uri]$Uri).AbsoluteUri
        status = 'failed'
        method = $Method.ToUpperInvariant()
        error = $_.Exception.Message
        latency_ms = $stopwatch.ElapsedMilliseconds
        tested_at = [DateTimeOffset]::Now.ToString('o')
    } | ConvertTo-Json
    exit 1
} finally {
    if ($null -ne $response) { $response.Dispose() }
    $client.Dispose()
    $handler.Dispose()
}
