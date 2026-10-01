# Restore the local scheduled preview and open the current design demo.
$ErrorActionPreference = 'Stop'
Start-ScheduledTask -TaskName 'FamilyWorkbench-LocalInvestmentPreview'
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    try {
        $response = Invoke-WebRequest -Uri 'http://127.0.0.1:4318/company.html' -UseBasicParsing -TimeoutSec 2
        if ($response.StatusCode -eq 200) {
            Start-Process 'http://127.0.0.1:4318/company.html'
            exit 0
        }
    } catch {}
    Start-Sleep -Seconds 1
}
throw 'Local preview did not start. Check .watch-local/preview-supervisor.log.'
