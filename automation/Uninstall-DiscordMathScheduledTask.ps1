[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$TaskName = 'Discord Math Research - Daily Import'
$TaskPath = '\'

Import-Module ScheduledTasks -ErrorAction Stop
$task = Get-ScheduledTask `
    -TaskName $TaskName `
    -TaskPath $TaskPath `
    -ErrorAction SilentlyContinue

if ($null -eq $task) {
    Write-Host "Scheduled task is not installed: $TaskName"
    return
}

if ($PSCmdlet.ShouldProcess(
        "$TaskPath$TaskName",
        'Unregister the exact scheduled task'
    )) {
    Unregister-ScheduledTask `
        -TaskName $TaskName `
        -TaskPath $TaskPath `
        -Confirm:$false
    Write-Host "Scheduled task removed: $TaskName"
    Write-Host 'Local data and the age identity file were not changed.'
}
