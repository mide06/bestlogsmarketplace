# Supplier Sync Scheduler

This project currently does not have an automatic scheduler configured in source control.

## What was added

- `tools/sync_supplier_products.bat`
- `tools/sync_supplier_products.ps1`

These scripts run the Django management command and write log output to `logs/sync_supplier_products.log`.

## Recommended automated schedule

### Windows Task Scheduler

1. Open Task Scheduler.
2. Create a new task named `BestLogMarketPlace Sync Supplier Products`.
3. Trigger: Daily, repeat task every `5 minutes` for a duration of `1 day`.
4. Action: Start a program.
   - Program/script: `C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`
   - Add arguments:
     `-ExecutionPolicy Bypass -File "C:\Users\HP\OneDrive\Documents\BestLogMarketPlaceProject\tools\sync_supplier_products.ps1"`
5. Conditions: disable `Start the task only if the computer is on AC power` if desired.
6. Settings: enable `Run task as soon as possible after a scheduled start is missed`.

### Linux / macOS cron example

```bash
*/5 * * * * cd /path/to/BestLogMarketPlaceProject && /usr/bin/python3 manage.py sync_supplier_products >> /path/to/BestLogMarketPlaceProject/logs/sync_supplier_products.log 2>&1
```

## How to verify it is running

- Check `logs/sync_supplier_products.log` for recent timestamps and successful sync output.
- Confirm the command does not create duplicate products by ensuring `supplier_product_id` remains unique in the database.
- Manually run:
  - `C:\Python313\python.exe manage.py sync_supplier_products`
  - or `python manage.py sync_supplier_products`

## Notes

- The code now marks products unavailable if `stock == 0` or `in_stock` is false.
- If stock returns later, the product is marked available again.
