$os = Get-CimInstance Win32_OperatingSystem
$totalGB = [math]::Round($os.TotalVisibleMemorySize/1MB,1)
$freeGB = [math]::Round($os.FreePhysicalMemory/1MB,1)
Write-Output ('RAM: ' + $totalGB + ' GB total, ' + $freeGB + ' GB free')
$clients = Get-Process Europa_Client -ErrorAction SilentlyContinue
$clients | Select-Object Id,@{n='RAM_MB';e={[math]::Round($_.WorkingSet64/1MB)}},@{n='CPU_s';e={[math]::Round($_.CPU,0)}} | Format-Table
$cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
$cores = (Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum
Write-Output ('CPU: ' + $cpu + '% load, ' + $cores + ' logical cores')
Get-CimInstance Win32_VideoController | Select-Object Name,@{n='VRAM_GB';e={[math]::Round($_.AdapterRAM/1GB,1)}} | Format-Table
