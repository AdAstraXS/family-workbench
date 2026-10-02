# Local preview supervisor. Run from the dedicated per-user scheduled task.
# No collection commands, migrations, production settings, or model calls.
$ErrorActionPreference = 'Stop'
$previewRoot = Split-Path -Parent $PSScriptRoot
$previewLogs = Join-Path $previewRoot '.watch-local'
[void](New-Item -ItemType Directory -Path $previewLogs -Force)
$previewNode = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
$previewServices = @(
    @{ Name = 'django'; Port = 4319; Exe = (Join-Path $previewRoot '.venv/Scripts/python.exe'); Directory = (Join-Path $previewRoot 'app'); Arguments = @('manage.py', 'runserver', '127.0.0.1:4319', '--settings=config.settings_watch_local', '--noreload') },
    @{ Name = 'demo'; Port = 4318; Exe = $previewNode; Directory = $previewRoot; Arguments = @('scripts/preview-investment-watch.mjs') }
)

function Test-PreviewPort([int]$Port) {
    $previewClient = New-Object System.Net.Sockets.TcpClient
    try {
        $connection = $previewClient.ConnectAsync('127.0.0.1', $Port)
        return ($connection.Wait(500) -and $previewClient.Connected)
    } catch { return $false } finally { $previewClient.Dispose() }
}

# Keep this task running so it can also restore a process that exits unexpectedly.
while ($true) {
    foreach ($service in $previewServices) {
        if (Test-PreviewPort $service.Port) { continue }
        try {
            if (-not (Test-Path -LiteralPath $service.Exe)) { throw "Missing runtime: $($service.Exe)" }
            $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
            $process = Start-Process -FilePath $service.Exe -ArgumentList $service.Arguments -WorkingDirectory $service.Directory -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $previewLogs "$($service.Name)-$stamp.out.log") -RedirectStandardError (Join-Path $previewLogs "$($service.Name)-$stamp.err.log")
            Add-Content -LiteralPath (Join-Path $previewLogs 'preview-supervisor.log') -Value "$stamp Started $($service.Name), PID $($process.Id), loopback port $($service.Port)."
        } catch {
            Add-Content -LiteralPath (Join-Path $previewLogs 'preview-supervisor.log') -Value "$(Get-Date -Format s) $($service.Name): $($_.Exception.Message)"
        }
    }
    Start-Sleep -Seconds 30
}
