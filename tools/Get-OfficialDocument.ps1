[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$SourceId,
    [Parameter(Mandatory=$true)][string]$Uri,
    [Parameter(Mandatory=$true)][string]$Title,
    [Parameter(Mandatory=$true)][string]$Publisher,
    [Parameter(Mandatory=$true)][DateTimeOffset]$PublishedAt,
    [Parameter(Mandatory=$true)][DateTimeOffset]$AvailableAt,
    [Parameter(Mandatory=$true)][string]$Locator,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [string]$RegistryPath,
    [string]$Language = 'zh-CN'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($RegistryPath)) { $RegistryPath = Join-Path $projectRoot 'specs\connectors\source-registry.v1.json' }
$registry = Get-Content -Raw -Encoding UTF8 $RegistryPath | ConvertFrom-Json
$sourceMatches = @($registry.sources | Where-Object source_id -eq $SourceId)
if ($sourceMatches.Count -ne 1) { throw "SourceId must resolve exactly once: $SourceId" }
$source = $sourceMatches[0]
if ($AvailableAt -lt $PublishedAt) { throw 'AvailableAt cannot precede PublishedAt.' }

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

$maxBytes = 52428800
$initialUri = [Uri]$Uri
$outputRoot = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null
$extension = [IO.Path]::GetExtension($initialUri.AbsolutePath)
if ([string]::IsNullOrWhiteSpace($extension) -or $extension.Length -gt 10) { $extension = '.bin' }
$safeName = ($SourceId -replace '[^A-Za-z0-9._-]', '_') + '-' + [Guid]::NewGuid().ToString('N') + $extension
$downloadPath = Join-Path $outputRoot $safeName
$stopwatch = [Diagnostics.Stopwatch]::StartNew()
$response = $null
$inputStream = $null
$outputStream = $null
Add-Type -AssemblyName System.Net.Http
$handler = New-Object Net.Http.HttpClientHandler
$handler.AllowAutoRedirect = $false
$client = New-Object Net.Http.HttpClient($handler)
$client.Timeout = [TimeSpan]::FromSeconds(60)
try {
    $current = $initialUri
    for ($redirects = 0; $redirects -le 3; $redirects++) {
        Assert-AllowedPublicUri $current
        $request = New-Object Net.Http.HttpRequestMessage([Net.Http.HttpMethod]::Get, $current)
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
        if ($response.Content.Headers.ContentLength.HasValue -and $response.Content.Headers.ContentLength.Value -gt $maxBytes) {
            throw "Document Content-Length exceeds $maxBytes bytes."
        }
        $inputStream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $outputStream = New-Object IO.FileStream($downloadPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $buffer = New-Object byte[] 65536
        [long]$written = 0
        while (($read = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            $written += $read
            if ($written -gt $maxBytes) { throw "Downloaded document exceeds $maxBytes bytes." }
            $outputStream.Write($buffer, 0, $read)
        }
        $outputStream.Flush()
        $outputStream.Dispose(); $outputStream = $null
        $inputStream.Dispose(); $inputStream = $null
        if ($written -le 0) { throw 'Downloaded document is empty.' }
        $stopwatch.Stop()
        $contentType = if ($null -ne $response.Content.Headers.ContentType) { $response.Content.Headers.ContentType.MediaType } else { $null }
        $hash = (Get-FileHash -LiteralPath $downloadPath -Algorithm SHA256).Hash
        $retrievedAt = [DateTimeOffset]::Now
        [ordered]@{
            connector_metadata = [ordered]@{
                connector_name = 'official_https_document_fetcher'; adapter_id = $source.adapter_id; adapter_version = '1.0.0'
                retrieved_at = $retrievedAt.ToString('o'); latency_ms = $stopwatch.ElapsedMilliseconds; raw_record_count = 1
                accepted_record_count = 1; discarded_record_count = 0; truncated = $false; retry_count = 0; source_ids = @($SourceId)
                redirect_count = $redirects
            }
            document = [ordered]@{
                document_id = 'CR.DOC.' + $hash.Substring(0,24); source_id = $SourceId; source_type = $source.source_family
                title = $Title; publisher = $Publisher; published_at = $PublishedAt.ToString('o'); available_at = $AvailableAt.ToString('o')
                retrieved_at = $retrievedAt.ToString('o'); source_url = $initialUri.AbsoluteUri; final_url = $current.AbsoluteUri; locator = $Locator
                content_hash = 'sha256:' + $hash.ToLowerInvariant(); license_tag = $source.license_status; access_class = $source.access_class
                evidence_tier = $source.evidence_tier; document_version = 'hash:' + $hash.Substring(0,12).ToLowerInvariant(); language = $Language
                content_type = $contentType; byte_length = $written; local_path = $downloadPath
            }
            quality_summary = [ordered]@{
                status = 'pass'; errors = @(); warnings = @(); coverage_ratio = 1.0; freshness_status = 'not_evaluated'
                license_status = $source.license_status; point_in_time_status = 'pass'; evidence_completeness_ratio = 1.0
            }
        } | ConvertTo-Json -Depth 12
        exit 0
    }
} catch {
    $stopwatch.Stop()
    if ($null -ne $outputStream) { $outputStream.Dispose() }
    if ($null -ne $inputStream) { $inputStream.Dispose() }
    if (Test-Path -LiteralPath $downloadPath) { Remove-Item -LiteralPath $downloadPath -Force }
    throw
} finally {
    if ($null -ne $response) { $response.Dispose() }
    $client.Dispose(); $handler.Dispose()
}
