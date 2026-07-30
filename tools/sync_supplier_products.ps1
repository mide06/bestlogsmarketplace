$projectDir = 'C:\Users\HP\OneDrive\Documents\BestLogMarketPlaceProject'
$python = 'C:\Python313\python.exe'
$logDir = Join-Path $projectDir 'logs'
$logFile = Join-Path $logDir 'sync_supplier_products.log'

if (-not (Test-Path $logDir)) {
    New-Item -Path $logDir -ItemType Directory | Out-Null
}

Set-Location $projectDir
& $python manage.py sync_supplier_products >> $logFile 2>&1
exit $LASTEXITCODE
