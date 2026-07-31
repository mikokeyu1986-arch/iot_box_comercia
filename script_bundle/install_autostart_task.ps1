param(
    [string]$TaskName = 'Partyday Custom IoT Box Native'
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$batPath = Join-Path $root 'start_native_iot_box.bat'
$runKeyPath = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$runKeyName = 'PartydayCustomIoTBoxNative'
$runValue = "cmd.exe /c `"$batPath`""

if (-not (Test-Path $batPath)) {
    throw "Missing startup script: $batPath"
}

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$batPath`""
$startupTrigger = New-ScheduledTaskTrigger -AtStartup
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew

$installMode = 'system'

try {
    try {
        $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $action `
            -Trigger @($startupTrigger, $logonTrigger) `
            -Principal $principal `
            -Settings $settings `
            -Description 'Autostart Partyday custom IoT Box native runtime'
    }
    catch {
        $installMode = 'user-logon'
        $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Highest
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $action `
            -Trigger $logonTrigger `
            -Principal $principal `
            -Settings $settings `
            -Description 'Autostart Partyday custom IoT Box native runtime'
    }
}
catch {
    $installMode = 'registry-run'
    if (-not (Test-Path $runKeyPath)) {
        New-Item -Path $runKeyPath -Force | Out-Null
    }
    New-ItemProperty -Path $runKeyPath -Name $runKeyName -Value $runValue -PropertyType String -Force | Out-Null
}

if ($installMode -ne 'registry-run') {
    Start-ScheduledTask -TaskName $TaskName
}
else {
    Start-Process -FilePath 'cmd.exe' -ArgumentList "/c `"$batPath`"" -WindowStyle Minimized
}
Write-Host "Installed autostart task ($installMode):" $TaskName
