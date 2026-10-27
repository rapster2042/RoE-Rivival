# Teardown helper (temporary): kill all RoE clients + services, report survivors.
$targets = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match 'Europa_Client' -or
    ($_.CommandLine -match 'run_service|windows_launch|LobbyEmulator|game_endpoint_probe|heartbeat_probe|RoE-Quad|RoE-Duo|apply_patches')
}
foreach ($p in $targets) {
    Write-Output ("KILL {0} {1} :: {2}" -f $p.ProcessId, $p.Name, $p.CommandLine.Substring(0,[Math]::Min(90,$p.CommandLine.Length)))
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 2
$left = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match 'Europa_Client' -or
    ($_.CommandLine -match 'run_service|windows_launch|LobbyEmulator|game_endpoint_probe|heartbeat_probe')
}
if ($left) { $left | ForEach-Object { Write-Output ("STILL ALIVE {0} {1}" -f $_.ProcessId, $_.Name) } }
else { Write-Output 'ALL CLEAR' }
netstat -ano | Select-String ':8001|:9001|:18001'
