param(
    [string]$TaskName = 'Partyday Custom IoT Box Native'
)

$ErrorActionPreference = 'Stop'

$runKeyPath = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$runKeyName = 'PartydayCustomIoTBoxNative'

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

if (Test-Path $runKeyPath) {
    Remove-ItemProperty -Path $runKeyPath -Name $runKeyName -ErrorAction SilentlyContinue
}

& (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'stop_native_iot_box.bat')

Write-Host "Removed autostart task:" $TaskName
