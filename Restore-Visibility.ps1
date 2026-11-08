$ErrorActionPreference = 'Stop'
# Undo exactly what PS5Haptics takeover mode changed in HidHide (used after a crash).
$receiptPath = Join-Path $PSScriptRoot 'isolation-restore.json'
if (-not (Test-Path -LiteralPath $receiptPath)) { Write-Host 'No session-owned visibility changes to restore.'; exit 0 }
$receipt = Get-Content -LiteralPath $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($device in $receipt.devices) {
    if ($device -notmatch '^(HID|USB)\\VID_054C&PID_(0CE6|0DF2)&MI_03\\') { throw 'Unexpected device in restore receipt' }
    & $receipt.cli --dev-unhide $device
    if ($LASTEXITCODE -ne 0) { throw 'Device restoration failed; keeping receipt' }
}
foreach ($app in $receipt.apps) { & $receipt.cli --app-unreg $app; if ($LASTEXITCODE -ne 0) { throw 'Whitelist restoration failed' } }
if ($receipt.restore_cloak_off) { & $receipt.cli --cloak-off; if ($LASTEXITCODE -ne 0) { throw 'Cloak restoration failed' } }
Remove-Item -LiteralPath $receiptPath
Write-Host 'Session-owned visibility changes restored.'
