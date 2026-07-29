[CmdletBinding()]
param(
    [ValidateSet('Handoff', 'Curate', 'Full')]
    [string]$Stage = 'Handoff',

    [datetime]$DailyAt = [datetime]::Today.AddHours(6).AddMinutes(30),

    [ValidateRange(1, 24)]
    [int]$PollEveryHours = 1,

    [switch]$AllowCommercialCuration,

    [switch]$AllowOpenWeightBatch,

    [switch]$WakeToRun,

    [switch]$RunNow,

    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$TaskName = 'Discord Math Research - Daily Import'
$TaskPath = '\'
$IdentityFileName = 'discord-export-key.txt'
$RequiredDataDirectories = @('logs', 'state', 'downloads', 'runs', 'staging')

function Get-NormalizedPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LiteralPath
    )

    $fullPath = [System.IO.Path]::GetFullPath($LiteralPath)
    $trimCharacters = [char[]]@(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )

    return $fullPath.TrimEnd($trimCharacters)
}

function Assert-ExactPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ActualPath,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedPath,

        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    if (-not [System.StringComparer]::OrdinalIgnoreCase.Equals(
            (Get-NormalizedPath -LiteralPath $ActualPath),
            (Get-NormalizedPath -LiteralPath $ExpectedPath)
        )) {
        throw "$Description is not the expected local path."
    }
}

function Assert-NotReparsePoint {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LiteralPath
    )

    $attributes = [System.IO.File]::GetAttributes($LiteralPath)
    if (($attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing to use a reparse point: $LiteralPath"
    }
}

function Get-SafeFileSystemTree {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RootPath
    )

    $normalizedRoot = Get-NormalizedPath -LiteralPath $RootPath
    Assert-NotReparsePoint -LiteralPath $normalizedRoot

    $directories = New-Object 'System.Collections.Generic.List[string]'
    $files = New-Object 'System.Collections.Generic.List[string]'
    $pending = New-Object 'System.Collections.Generic.Queue[string]'
    $directories.Add($normalizedRoot)
    $pending.Enqueue($normalizedRoot)

    $rootPrefix = $normalizedRoot + [System.IO.Path]::DirectorySeparatorChar
    while ($pending.Count -gt 0) {
        $directory = $pending.Dequeue()
        foreach ($entry in [System.IO.Directory]::GetFileSystemEntries($directory)) {
            $normalizedEntry = Get-NormalizedPath -LiteralPath $entry
            if (-not $normalizedEntry.StartsWith(
                    $rootPrefix,
                    [System.StringComparison]::OrdinalIgnoreCase
                )) {
                throw "A path escaped the protected data directory: $normalizedEntry"
            }

            Assert-NotReparsePoint -LiteralPath $normalizedEntry
            $attributes = [System.IO.File]::GetAttributes($normalizedEntry)
            if (($attributes -band [System.IO.FileAttributes]::Directory) -ne 0) {
                $directories.Add($normalizedEntry)
                $pending.Enqueue($normalizedEntry)
            }
            else {
                $files.Add($normalizedEntry)
            }
        }
    }

    return [pscustomobject]@{
        Directories = $directories.ToArray()
        Files = $files.ToArray()
    }
}

function New-RestrictedDirectorySecurity {
    param(
        [Parameter(Mandatory = $true)]
        [System.Security.Principal.SecurityIdentifier]$OwnerSid,

        [Parameter(Mandatory = $true)]
        [System.Security.Principal.SecurityIdentifier[]]$AllowedSids
    )

    $security = New-Object System.Security.AccessControl.DirectorySecurity
    $security.SetAccessRuleProtection($true, $false)
    $security.SetOwner($OwnerSid)

    foreach ($sid in $AllowedSids) {
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $sid,
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            (
                [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
                [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
            ),
            [System.Security.AccessControl.PropagationFlags]::None,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        [void]$security.AddAccessRule($rule)
    }

    return $security
}

function New-RestrictedFileSecurity {
    param(
        [Parameter(Mandatory = $true)]
        [System.Security.Principal.SecurityIdentifier]$OwnerSid,

        [Parameter(Mandatory = $true)]
        [System.Security.Principal.SecurityIdentifier[]]$AllowedSids
    )

    $security = New-Object System.Security.AccessControl.FileSecurity
    $security.SetAccessRuleProtection($true, $false)
    $security.SetOwner($OwnerSid)

    foreach ($sid in $AllowedSids) {
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $sid,
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            [System.Security.AccessControl.InheritanceFlags]::None,
            [System.Security.AccessControl.PropagationFlags]::None,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        [void]$security.AddAccessRule($rule)
    }

    return $security
}

function Assert-RestrictedAcl {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LiteralPath,

        [Parameter(Mandatory = $true)]
        [System.Security.Principal.SecurityIdentifier[]]$AllowedSids,

        [Parameter(Mandatory = $true)]
        [ValidateSet('Directory', 'File')]
        [string]$PathType
    )

    if ($PathType -eq 'Directory') {
        $security = [System.IO.Directory]::GetAccessControl(
            $LiteralPath,
            [System.Security.AccessControl.AccessControlSections]::Access
        )
    }
    else {
        $security = [System.IO.File]::GetAccessControl(
            $LiteralPath,
            [System.Security.AccessControl.AccessControlSections]::Access
        )
    }

    if (-not $security.AreAccessRulesProtected) {
        throw "ACL inheritance is still enabled: $LiteralPath"
    }

    $allowedSidValues = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    foreach ($sid in $AllowedSids) {
        [void]$allowedSidValues.Add($sid.Value)
    }

    $observedSidValues = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    $rules = $security.GetAccessRules(
        $true,
        $true,
        [System.Security.Principal.SecurityIdentifier]
    )
    foreach ($rule in $rules) {
        $sidValue = $rule.IdentityReference.Value
        if (
            $rule.IsInherited -or
            $rule.AccessControlType -ne
                [System.Security.AccessControl.AccessControlType]::Allow -or
            -not $allowedSidValues.Contains($sidValue)
        ) {
            throw "Unexpected ACL entry remains on: $LiteralPath"
        }
        [void]$observedSidValues.Add($sidValue)
    }

    foreach ($sid in $AllowedSids) {
        if (-not $observedSidValues.Contains($sid.Value)) {
            throw "A required ACL principal is missing on: $LiteralPath"
        }
    }
}

function Protect-DataTree {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RootPath,

        [Parameter(Mandatory = $true)]
        [System.Security.Principal.SecurityIdentifier]$CurrentUserSid
    )

    $systemSid = [System.Security.Principal.SecurityIdentifier]::new(
        [System.Security.Principal.WellKnownSidType]::LocalSystemSid,
        $null
    )
    $administratorsSid = [System.Security.Principal.SecurityIdentifier]::new(
        [System.Security.Principal.WellKnownSidType]::BuiltinAdministratorsSid,
        $null
    )
    $allowedSids = @($CurrentUserSid, $systemSid, $administratorsSid)

    # Inspect the complete tree before changing any ACL. Reparse points are never
    # followed, so an attacker-controlled link cannot redirect ACL changes.
    $tree = Get-SafeFileSystemTree -RootPath $RootPath

    foreach ($directory in $tree.Directories) {
        $security = New-RestrictedDirectorySecurity `
            -OwnerSid $CurrentUserSid `
            -AllowedSids $allowedSids
        [System.IO.Directory]::SetAccessControl($directory, $security)
    }

    foreach ($file in $tree.Files) {
        $security = New-RestrictedFileSecurity `
            -OwnerSid $CurrentUserSid `
            -AllowedSids $allowedSids
        [System.IO.File]::SetAccessControl($file, $security)
    }

    foreach ($directory in $tree.Directories) {
        Assert-RestrictedAcl `
            -LiteralPath $directory `
            -AllowedSids $allowedSids `
            -PathType Directory
    }
    foreach ($file in $tree.Files) {
        Assert-RestrictedAcl `
            -LiteralPath $file `
            -AllowedSids $allowedSids `
            -PathType File
    }
}

if (
    $Stage -in @('Curate', 'Full') -and
    -not $AllowCommercialCuration.IsPresent
) {
    throw (
        "Stage '$Stage' requires -AllowCommercialCuration because it can invoke " +
        'paid commercial providers.'
    )
}
if ($Stage -eq 'Full' -and -not $AllowOpenWeightBatch.IsPresent) {
    throw (
        "Stage 'Full' requires -AllowOpenWeightBatch because it launches the " +
        'high-volume research batch.'
    )
}

$windowsIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
if ($null -eq $windowsIdentity.User) {
    throw 'The current Windows user SID could not be determined.'
}

$localApplicationData = [System.Environment]::GetFolderPath(
    [System.Environment+SpecialFolder]::LocalApplicationData
)
if ([string]::IsNullOrWhiteSpace($localApplicationData)) {
    throw 'The local application-data directory could not be determined.'
}

$expectedParentRoot = Get-NormalizedPath -LiteralPath (
    Join-Path $localApplicationData 'DiscordMathResearch'
)
$parentRoot = $expectedParentRoot
$expectedDataRoot = Get-NormalizedPath -LiteralPath (
    Join-Path $expectedParentRoot 'automation'
)
$dataRoot = $expectedDataRoot
$expectedIdentityPath = Get-NormalizedPath -LiteralPath (
    Join-Path $expectedParentRoot $IdentityFileName
)
$identityPath = $expectedIdentityPath

Assert-ExactPath `
    -ActualPath $parentRoot `
    -ExpectedPath $expectedParentRoot `
    -Description 'Discord Math parent data directory'
Assert-ExactPath `
    -ActualPath $dataRoot `
    -ExpectedPath $expectedDataRoot `
    -Description 'Discord Math automation data directory'
Assert-ExactPath `
    -ActualPath $identityPath `
    -ExpectedPath $expectedIdentityPath `
    -Description 'age identity file'

[void][System.IO.Directory]::CreateDirectory($parentRoot)
Assert-NotReparsePoint -LiteralPath $parentRoot

# Lock the dedicated parent immediately. This also prevents a subsequently
# created identity file from inheriting permissive ACLs if setup stops here.
Protect-DataTree `
    -RootPath $parentRoot `
    -CurrentUserSid $windowsIdentity.User

if (-not [System.IO.File]::Exists($identityPath)) {
    throw (
        "The required age identity file does not exist at '$identityPath'. " +
        'Create it before installing the scheduled task.'
    )
}
Assert-NotReparsePoint -LiteralPath $identityPath

$importScriptPath = Get-NormalizedPath -LiteralPath (
    Join-Path $PSScriptRoot 'Invoke-DiscordMathImport.ps1'
)
if (-not [System.IO.File]::Exists($importScriptPath)) {
    throw "The import script does not exist at '$importScriptPath'."
}
Assert-NotReparsePoint -LiteralPath $importScriptPath

Import-Module ScheduledTasks -ErrorAction Stop
$existingTask = Get-ScheduledTask `
    -TaskName $TaskName `
    -TaskPath $TaskPath `
    -ErrorAction SilentlyContinue
if ($null -ne $existingTask -and -not $Force.IsPresent) {
    throw (
        "The scheduled task '$TaskName' already exists. Use -Force to replace " +
        'that exact task.'
    )
}

# Lock the existing parent tree again immediately before creating runtime
# directories. This includes the private identity without ever opening or
# printing that file.
Protect-DataTree `
    -RootPath $parentRoot `
    -CurrentUserSid $windowsIdentity.User

[void][System.IO.Directory]::CreateDirectory($dataRoot)
foreach ($directoryName in $RequiredDataDirectories) {
    [void][System.IO.Directory]::CreateDirectory(
        (Join-Path $dataRoot $directoryName)
    )
}

# Apply and verify the restricted ACL on the newly created runtime directories.
Protect-DataTree `
    -RootPath $parentRoot `
    -CurrentUserSid $windowsIdentity.User

$powerShellPath = Get-NormalizedPath -LiteralPath (
    Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
)
if (-not [System.IO.File]::Exists($powerShellPath)) {
    throw "Windows PowerShell was not found at '$powerShellPath'."
}

$actionArguments = @(
    '-NoLogo',
    '-NoProfile',
    '-NonInteractive',
    '-WindowStyle', 'Hidden',
    '-ExecutionPolicy', 'Bypass',
    '-File', ('"{0}"' -f $importScriptPath),
    '-Stage', $Stage
)
if ($Stage -in @('Curate', 'Full')) {
    $actionArguments += '-AllowCommercialCuration'
}
if ($Stage -eq 'Full') {
    $actionArguments += '-AllowOpenWeightBatch'
}

$repositoryRoot = Get-NormalizedPath -LiteralPath (
    Join-Path $PSScriptRoot '..'
)
$action = New-ScheduledTaskAction `
    -Execute $powerShellPath `
    -Argument ($actionArguments -join ' ') `
    -WorkingDirectory $repositoryRoot
$triggers = @()
$firstTrigger = [datetime]::Today.Add($DailyAt.TimeOfDay)
for ($offset = 0; $offset -lt 24; $offset += $PollEveryHours) {
    $triggers += New-ScheduledTaskTrigger `
        -Daily `
        -At $firstTrigger.AddHours($offset)
}
$principal = New-ScheduledTaskPrincipal `
    -UserId $windowsIdentity.Name `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 20) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -WakeToRun:([bool]$WakeToRun)

$registrationParameters = @{
    TaskName = $TaskName
    TaskPath = $TaskPath
    Action = $action
    Trigger = $triggers
    Principal = $principal
    Settings = $settings
    Description = (
        'Downloads the latest encrypted Discord Mathematics export, validates ' +
        'and decrypts it locally, and hands it to The Agentic Researcher.'
    )
}
if ($Force.IsPresent) {
    $registrationParameters.Force = $true
}

Register-ScheduledTask @registrationParameters | Out-Null

if ($RunNow.IsPresent) {
    Start-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath
}

Write-Host "Scheduled task installed: $TaskName"
Write-Host (
    "Schedule: every {0} hour(s), starting daily at {1:HH:mm} local time" -f
    $PollEveryHours, $DailyAt
)
Write-Host "Stage: $Stage"
Write-Host "Import script: $importScriptPath"
Write-Host "Protected local data: $parentRoot"
if ($RunNow.IsPresent) {
    Write-Host 'The first run has been started.'
}
