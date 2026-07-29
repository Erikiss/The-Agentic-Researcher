[CmdletBinding()]
param(
    [ValidateSet("Handoff", "Curate", "Full")]
    [string]$Stage = "Handoff",

    [string]$Repository = "Erikiss/Discord-Mathematics-Early-University",
    [string]$Workflow = "daily-crawl.yml",
    [string]$Branch = "main",

    [string]$DataRoot = (Join-Path $env:LOCALAPPDATA "DiscordMathResearch\automation"),
    [string]$IdentityFile = (Join-Path $env:LOCALAPPDATA "DiscordMathResearch\discord-export-key.txt"),
    [string]$AgenticResearcherRoot,
    [string]$DiscordRepositoryRoot,

    [string]$EncryptedArtifact,
    [string]$RunId,

    [string]$GhExecutable,
    [string]$AgeExecutable,
    [string]$PythonExecutable = "python",

    [switch]$AllowCommercialCuration,
    [switch]$AllowOpenWeightBatch,
    [switch]$SkipAclValidationForOfflineTest,

    [ValidateSet("opencode")]
    [string]$OpenWeightProvider = "opencode",
    [string]$OpenWeightCommand = "opencode run --auto {prompt}",
    [ValidateRange(0, 2147483647)]
    [int]$MaxTasks = 0,
    [ValidateRange(1, 20)]
    [int]$MaxAttempts = 2,
    [ValidateRange(1, 86400)]
    [int]$ProviderTimeoutSeconds = 7200
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:ArtifactNamePattern = "^discord-math-export-\d{4}-\d{2}-\d{2}$"
$script:EncryptedFileName = "discord-math-export.tar.gz.age"
$script:DataRootFull = $null
$script:LogPath = $null

function Get-UtcTimestamp {
    return [DateTime]::UtcNow.ToString("o")
}

function Resolve-FullPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    return [System.IO.Path]::GetFullPath($Path)
}

function Test-IsChildPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Parent,
        [Parameter(Mandatory = $true)]
        [string]$Child
    )

    $parentFull = (Resolve-FullPath -Path $Parent).TrimEnd(
        [char[]]@(
            [System.IO.Path]::DirectorySeparatorChar,
            [System.IO.Path]::AltDirectorySeparatorChar
        )
    )
    $childFull = Resolve-FullPath -Path $Child
    $prefix = $parentFull + [System.IO.Path]::DirectorySeparatorChar
    return $childFull.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
}

function Remove-PrivateTree {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    if (-not (Test-IsChildPath -Parent $script:DataRootFull -Child $Path)) {
        throw "Refusing to remove a path outside the private data root."
    }
    Remove-Item -LiteralPath $Path -Recurse -Force
}

function Write-ImportLog {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    $line = "{0} {1}" -f (Get-UtcTimestamp), $Message
    Write-Host $line
    if ($null -ne $script:LogPath) {
        Add-Content -LiteralPath $script:LogPath -Value $line -Encoding UTF8
    }
}

function Write-JsonAtomic {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Value,
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $directory = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        throw "JSON destination directory does not exist: $directory"
    }
    $temporary = Join-Path $directory (
        ".{0}.{1}.tmp" -f ([System.IO.Path]::GetFileName($Path)), [Guid]::NewGuid().ToString("N")
    )
    $backup = $null
    try {
        $json = $Value | ConvertTo-Json -Depth 32
        $utf8WithoutBom = New-Object -TypeName System.Text.UTF8Encoding `
            -ArgumentList $false
        [System.IO.File]::WriteAllText(
            $temporary,
            $json + [Environment]::NewLine,
            $utf8WithoutBom
        )
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            $backup = Join-Path $directory (
                ".{0}.{1}.bak" -f
                ([System.IO.Path]::GetFileName($Path)),
                [Guid]::NewGuid().ToString("N")
            )
            [System.IO.File]::Replace($temporary, $Path, $backup, $true)
        }
        else {
            [System.IO.File]::Move($temporary, $Path)
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
        if ($null -ne $backup -and (Test-Path -LiteralPath $backup)) {
            Remove-Item -LiteralPath $backup -Force
        }
    }
}

function Read-JsonFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required JSON file not found: $Path"
    }
    return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Resolve-CommandPath {
    param(
        [string]$Requested,
        [Parameter(Mandatory = $true)]
        [string]$CommandName,
        [string[]]$Fallbacks = @()
    )

    if (-not [string]::IsNullOrWhiteSpace($Requested)) {
        if (Test-Path -LiteralPath $Requested -PathType Leaf) {
            return (Resolve-FullPath -Path $Requested)
        }
        $requestedCommand = Get-Command $Requested -CommandType Application -ErrorAction SilentlyContinue
        if ($null -ne $requestedCommand) {
            return $requestedCommand.Source
        }
        throw "Executable not found: $Requested"
    }

    $command = Get-Command $CommandName -CommandType Application -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }
    foreach ($fallback in $Fallbacks) {
        if (-not [string]::IsNullOrWhiteSpace($fallback) -and
            (Test-Path -LiteralPath $fallback -PathType Leaf)) {
            return (Resolve-FullPath -Path $fallback)
        }
    }
    throw "Executable '$CommandName' was not found."
}

function Invoke-CapturedCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [Parameter(Mandatory = $true)]
        [string]$TemporaryDirectory,
        [string]$FailureLabel = "External command"
    )

    $stderrPath = Join-Path $TemporaryDirectory (
        ".stderr-{0}.txt" -f [Guid]::NewGuid().ToString("N")
    )
    try {
        $output = @(& $Executable @Arguments 2> $stderrPath)
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            $detail = ""
            if (Test-Path -LiteralPath $stderrPath -PathType Leaf) {
                $detail = ([string](
                        Get-Content -LiteralPath $stderrPath -Raw -Encoding UTF8
                    )).Trim()
            }
            if ($detail.Length -gt 2000) {
                $detail = $detail.Substring(0, 2000)
            }
            if ([string]::IsNullOrWhiteSpace($detail)) {
                throw "$FailureLabel failed with exit code $exitCode."
            }
            throw "$FailureLabel failed with exit code ${exitCode}: $detail"
        }
        return ($output -join [Environment]::NewLine)
    }
    finally {
        if (Test-Path -LiteralPath $stderrPath) {
            Remove-Item -LiteralPath $stderrPath -Force
        }
    }
}

function Invoke-PythonModule {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [Parameter(Mandatory = $true)]
        [string]$WorkingDirectory,
        [Parameter(Mandatory = $true)]
        [string]$FailureLabel
    )

    Push-Location -LiteralPath $WorkingDirectory
    $previousErrorActionPreference = $ErrorActionPreference
    $capturedOutput = Join-Path $script:StagingRoot (
        ".python-output-{0}.txt" -f [Guid]::NewGuid().ToString("N")
    )
    $protectedFailureOutput = $null
    try {
        $ErrorActionPreference = "Continue"
        & $script:PythonPath @Arguments 2>&1 |
            Out-File -LiteralPath $capturedOutput -Encoding UTF8
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            if (Test-Path -LiteralPath $capturedOutput -PathType Leaf) {
                $protectedFailureOutput = Join-Path $script:LogsRoot (
                    "python-failure-{0}-{1}.log" -f
                    [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ"),
                    [Guid]::NewGuid().ToString("N")
                )
                Move-Item -LiteralPath $capturedOutput `
                    -Destination $protectedFailureOutput
            }
            if ($null -ne $protectedFailureOutput) {
                throw (
                    "$FailureLabel failed with exit code $exitCode. " +
                    "Private details were retained at: $protectedFailureOutput"
                )
            }
            throw "$FailureLabel failed with exit code $exitCode."
        }
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
        if (Test-Path -LiteralPath $capturedOutput) {
            Remove-Item -LiteralPath $capturedOutput -Force
        }
        Pop-Location
    }
}

function Assert-NoReparsePoint {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label must not be a symbolic link, junction, or other reparse point."
    }
}

function Assert-PrivateAcl {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$Label,
        [switch]$RequireProtected
    )

    $allowedSids = @(
        [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value,
        "S-1-5-18",      # LocalSystem
        "S-1-5-32-544"   # Builtin Administrators
    )
    $acl = Get-Acl -LiteralPath $Path
    if ($RequireProtected -and -not $acl.AreAccessRulesProtected) {
        throw "$Label inherits access rules. Run the task installer to protect its ACL."
    }
    try {
        $ownerSid = $acl.GetOwner(
            [System.Security.Principal.SecurityIdentifier]
        ).Value
    }
    catch {
        throw "$Label has an owner whose Windows identity cannot be verified."
    }
    if ($allowedSids -notcontains $ownerSid) {
        throw "$Label is owned by an identity outside the current user, LocalSystem, and Administrators."
    }
    foreach ($rule in $acl.Access) {
        if ($rule.AccessControlType -ne
            [System.Security.AccessControl.AccessControlType]::Allow) {
            continue
        }
        try {
            $sid = $rule.IdentityReference.Translate(
                [System.Security.Principal.SecurityIdentifier]
            ).Value
        }
        catch {
            throw "$Label contains an access rule whose Windows identity cannot be verified."
        }
        if ($allowedSids -notcontains $sid) {
            throw "$Label grants access to an identity outside the current user, LocalSystem, and Administrators. Run the task installer to harden its ACL."
        }
    }
}

function Assert-PrivateStorage {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,
        [Parameter(Mandatory = $true)]
        [string]$Identity,
        [switch]$SkipAcl
    )

    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw "LOCALAPPDATA is not available."
    }
    $localAppDataFull = Resolve-FullPath -Path $env:LOCALAPPDATA
    if (-not (Test-IsChildPath -Parent $localAppDataFull -Child $Root)) {
        throw "DataRoot must be a child of LOCALAPPDATA."
    }
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        throw "Private DataRoot does not exist. Run the task installer first: $Root"
    }
    if (-not (Test-Path -LiteralPath $Identity -PathType Leaf)) {
        throw "The local age identity file is missing."
    }
    if ((Get-Item -LiteralPath $Identity).Length -le 0) {
        throw "The local age identity file is empty."
    }

    Assert-NoReparsePoint -Path $Root -Label "DataRoot"
    Assert-NoReparsePoint -Path $Identity -Label "Identity file"
    if (-not $SkipAcl) {
        $privateParent = Split-Path -Parent $Root
        $identityParent = Split-Path -Parent $Identity
        Assert-NoReparsePoint -Path $privateParent -Label "Private storage parent"
        Assert-NoReparsePoint -Path $identityParent -Label "Identity directory"
        Assert-PrivateAcl -Path $privateParent -Label "Private storage parent" `
            -RequireProtected
        Assert-PrivateAcl -Path $identityParent -Label "Identity directory" `
            -RequireProtected
        Assert-PrivateAcl -Path $Root -Label "DataRoot"
        Assert-PrivateAcl -Path $Identity -Label "Identity file" `
            -RequireProtected
    }
}

function Assert-RepositoryCheckout {
    param(
        [Parameter(Mandatory = $true)]
        [string]$AgenticRoot,
        [Parameter(Mandatory = $true)]
        [string]$DiscordRoot
    )

    $agenticInstructions = Join-Path $AgenticRoot "INSTRUCTIONS.md"
    $secureModule = Join-Path $AgenticRoot "agentic_researcher\secure_artifacts.py"
    $contractSmoke = Join-Path $DiscordRoot "scripts\contract_smoke.py"
    if (-not (Test-Path -LiteralPath $agenticInstructions -PathType Leaf) -or
        -not (Test-Path -LiteralPath $secureModule -PathType Leaf)) {
        throw "Agentic Researcher checkout is incomplete: $AgenticRoot"
    }
    if (-not (Test-Path -LiteralPath $contractSmoke -PathType Leaf)) {
        throw "Discord Mathematics checkout is incomplete: $DiscordRoot"
    }
    Assert-NoReparsePoint -Path $AgenticRoot -Label "Agentic Researcher checkout"
    Assert-NoReparsePoint -Path $DiscordRoot -Label "Discord Mathematics checkout"
}

function Get-RunArtifacts {
    param(
        [Parameter(Mandatory = $true)]
        [long]$GitHubRunId
    )

    $allArtifacts = @()
    $page = 1
    do {
        $endpoint = "repos/$Repository/actions/runs/$GitHubRunId/artifacts?per_page=100&page=$page"
        $json = Invoke-CapturedCommand -Executable $script:GhPath `
            -Arguments @("api", "--method", "GET", $endpoint) `
            -TemporaryDirectory $script:StagingRoot `
            -FailureLabel "GitHub artifact lookup"
        $response = $json | ConvertFrom-Json
        $pageArtifacts = @($response.artifacts)
        $allArtifacts += $pageArtifacts
        $page += 1
    } while ($allArtifacts.Count -lt [int]$response.total_count -and
        $pageArtifacts.Count -gt 0)
    return @($allArtifacts)
}

function Find-LatestArtifact {
    $json = Invoke-CapturedCommand -Executable $script:GhPath `
        -Arguments @(
            "run", "list",
            "--repo", $Repository,
            "--workflow", $Workflow,
            "--branch", $Branch,
            "--status", "success",
            "--limit", "100",
            "--json", "databaseId,attempt,createdAt,headSha,url"
        ) `
        -TemporaryDirectory $script:StagingRoot `
        -FailureLabel "GitHub workflow run lookup"
    # Windows PowerShell 5.1 wraps an inline `ConvertFrom-Json` result for `[]`
    # as one nested empty array. Assign first so `@(...)` normalizes it to zero
    # runs instead of trying to sort an object without `createdAt`.
    $parsedRuns = $json | ConvertFrom-Json
    $runs = @($parsedRuns)
    $orderedRuns = @($runs | Sort-Object {
            [DateTimeOffset]::Parse([string]$_.createdAt)
        } -Descending)

    foreach ($run in $orderedRuns) {
        $artifacts = @(Get-RunArtifacts -GitHubRunId ([long]$run.databaseId))
        $matching = @(
            $artifacts | Where-Object {
                ([string]$_.name -cmatch $script:ArtifactNamePattern) -and
                ($_.expired -eq $false) -and
                ([long]$_.size_in_bytes -gt 0) -and
                ([DateTimeOffset]::Parse([string]$_.expires_at) -gt [DateTimeOffset]::UtcNow)
            }
        )
        if ($matching.Count -gt 1) {
            throw "Workflow run $($run.databaseId) contains multiple eligible Discord Math artifacts."
        }
        if ($matching.Count -eq 1) {
            $artifactDigest = $null
            if ($null -ne $matching[0].PSObject.Properties["digest"]) {
                $artifactDigest = [string]$matching[0].digest
            }
            return [PSCustomObject]@{
                RunId = [string]$run.databaseId
                Attempt = [int]$run.attempt
                RunCreatedAt = [string]$run.createdAt
                HeadSha = [string]$run.headSha
                RunUrl = [string]$run.url
                ArtifactId = [string]([long]$matching[0].id)
                ArtifactName = [string]$matching[0].name
                ArtifactSize = [long]$matching[0].size_in_bytes
                ArtifactDigest = $artifactDigest
                ArtifactCreatedAt = [string]$matching[0].created_at
                ArtifactExpiresAt = [string]$matching[0].expires_at
            }
        }
    }
    throw "No successful $Workflow run on $Branch has a non-expired artifact matching $script:ArtifactNamePattern."
}

function Get-EncryptedArtifact {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Source
    )

    $downloadKey = "run-{0}-artifact-{1}" -f $Source.RunId, $Source.ArtifactId
    $finalDirectory = Join-Path $script:DownloadsRoot $downloadKey
    $finalFile = Join-Path $finalDirectory $script:EncryptedFileName

    if (Test-Path -LiteralPath $finalDirectory) {
        Assert-NoReparsePoint -Path $finalDirectory -Label "Downloaded artifact directory"
        $existingFiles = @(Get-ChildItem -LiteralPath $finalDirectory -File -Recurse -Force)
        if ($existingFiles.Count -ne 1 -or
            $existingFiles[0].Name -cne $script:EncryptedFileName -or
            $existingFiles[0].Length -le 0) {
            throw "Existing artifact cache is invalid: $finalDirectory"
        }
        if ((Resolve-FullPath -Path $existingFiles[0].Directory.FullName) -cne
            (Resolve-FullPath -Path $finalDirectory)) {
            throw "Existing encrypted artifact must be directly at the cache root."
        }
        Assert-NoReparsePoint -Path $existingFiles[0].FullName `
            -Label "Cached encrypted artifact"
        if ($Source.Mode -eq "offline") {
            $cachedHash = (Get-FileHash -LiteralPath $finalFile -Algorithm SHA256).Hash
            $sourceHash = (Get-FileHash -LiteralPath $Source.LocalPath -Algorithm SHA256).Hash
            if ($cachedHash -cne $sourceHash) {
                throw "Offline RunId already refers to a different encrypted artifact SHA256."
            }
        }
        return $finalFile
    }

    $incoming = Join-Path $script:StagingRoot (
        "download-{0}" -f [Guid]::NewGuid().ToString("N")
    )
    New-Item -ItemType Directory -Path $incoming | Out-Null
    try {
        if ($Source.Mode -eq "offline") {
            Copy-Item -LiteralPath $Source.LocalPath `
                -Destination (Join-Path $incoming $script:EncryptedFileName)
        }
        else {
            Invoke-CapturedCommand -Executable $script:GhPath `
                -Arguments @(
                    "run", "download", [string]$Source.RunId,
                    "--repo", $Repository,
                    "--name", [string]$Source.ArtifactName,
                    "--dir", $incoming
                ) `
                -TemporaryDirectory $script:StagingRoot `
                -FailureLabel "GitHub artifact download" | Out-Null
        }

        $files = @(Get-ChildItem -LiteralPath $incoming -File -Recurse -Force)
        if ($files.Count -ne 1 -or
            $files[0].Name -cne $script:EncryptedFileName -or
            $files[0].Length -le 0) {
            throw "The artifact must contain exactly one non-empty $script:EncryptedFileName."
        }
        $relativeParent = $files[0].Directory.FullName
        if ((Resolve-FullPath -Path $relativeParent) -cne (Resolve-FullPath -Path $incoming)) {
            throw "The encrypted artifact file must be at the artifact root."
        }
        Assert-NoReparsePoint -Path $files[0].FullName `
            -Label "Downloaded encrypted artifact"
        [System.IO.Directory]::Move($incoming, $finalDirectory)
    }
    finally {
        if (Test-Path -LiteralPath $incoming) {
            Remove-PrivateTree -Path $incoming
        }
    }
    return $finalFile
}

function Assert-HandoffFiles {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ExtractionRoot
    )

    $exports = Join-Path $ExtractionRoot "discord_exports"
    $bundle = Join-Path $exports "ingest_bundle.json"
    $commitFile = Join-Path $exports "AGENTIC_RESEARCHER_COMMIT.txt"
    $mediaRoot = Join-Path $exports "curation_media"
    $mediaManifest = Join-Path $mediaRoot "media_manifest.json"
    if (-not (Test-Path -LiteralPath $exports -PathType Container)) {
        throw "Extracted artifact is missing the required discord_exports directory."
    }
    if (-not (Test-Path -LiteralPath $bundle -PathType Leaf) -or
        (Get-Item -LiteralPath $bundle).Length -le 0) {
        throw "Extracted artifact is missing a non-empty discord_exports\ingest_bundle.json."
    }
    if (-not (Test-Path -LiteralPath $commitFile -PathType Leaf)) {
        throw "Extracted artifact is missing discord_exports\AGENTIC_RESEARCHER_COMMIT.txt."
    }
    $agenticCommit = ([string](
            Get-Content -LiteralPath $commitFile -Raw -Encoding ASCII
        )).Trim()
    if ($agenticCommit -cnotmatch "^[0-9a-f]{40}$") {
        throw "AGENTIC_RESEARCHER_COMMIT.txt must contain exactly one 40-character lowercase Git commit."
    }
    if (-not (Test-Path -LiteralPath $mediaRoot -PathType Container) -or
        -not (Test-Path -LiteralPath $mediaManifest -PathType Leaf) -or
        (Get-Item -LiteralPath $mediaManifest).Length -le 0) {
        throw "Extracted artifact is missing a non-empty curation_media\media_manifest.json."
    }
    $bundleDocument = Read-JsonFile -Path $bundle
    $mediaManifestDocument = Read-JsonFile -Path $mediaManifest
    if ($null -eq $bundleDocument.PSObject.Properties["bundle_id"] -or
        [string]::IsNullOrWhiteSpace([string]$bundleDocument.bundle_id)) {
        throw "ingest_bundle.json does not contain a valid bundle_id."
    }
    if ($null -eq
        $mediaManifestDocument.PSObject.Properties["schema_version"] -or
        [string]$mediaManifestDocument.schema_version -cne
        "agentic-researcher/media-manifest/v1") {
        throw "media_manifest.json uses an unsupported schema_version."
    }
    if ($null -eq $mediaManifestDocument.PSObject.Properties["bundle_id"] -or
        [string]$mediaManifestDocument.bundle_id -cne
        [string]$bundleDocument.bundle_id) {
        throw "media_manifest.json does not refer to the extracted bundle_id."
    }
    return [PSCustomObject]@{
        Exports = $exports
        Bundle = $bundle
        BundleId = [string]$bundleDocument.bundle_id
        BundleSha256 = (
            Get-FileHash -LiteralPath $bundle -Algorithm SHA256
        ).Hash.ToLowerInvariant()
        AgenticResearcherCommit = $agenticCommit
        MediaRoot = $mediaRoot
        MediaManifest = $mediaManifest
        MediaManifestSha256 = (
            Get-FileHash -LiteralPath $mediaManifest -Algorithm SHA256
        ).Hash.ToLowerInvariant()
    }
}

function Invoke-ContractSmoke {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Handoff
    )

    $arguments = @(
        (Join-Path $script:DiscordRoot "scripts\contract_smoke.py"),
        "--agentic-researcher", $script:AgenticRoot,
        "--bundle", $Handoff.Bundle
    )
    if (Test-Path -LiteralPath $Handoff.MediaRoot -PathType Container) {
        $arguments += @("--media-root", $Handoff.MediaRoot)
    }
    Invoke-PythonModule -Arguments $arguments `
        -WorkingDirectory $script:DiscordRoot `
        -FailureLabel "Discord-to-Agentic contract validation"
}

function Test-HandoffReceipt {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ReceiptPath,
        [Parameter(Mandatory = $true)]
        [object]$Source,
        [Parameter(Mandatory = $true)]
        [string]$ArtifactHash
    )

    if (-not (Test-Path -LiteralPath $ReceiptPath -PathType Leaf)) {
        return $false
    }
    $receipt = Read-JsonFile -Path $ReceiptPath
    if ([string]$receipt.run_id -cne [string]$Source.RunId -or
        [string]$receipt.artifact_id -cne [string]$Source.ArtifactId -or
        [string]$receipt.artifact_sha256 -cne $ArtifactHash) {
        throw "Existing run receipt does not match the selected run, artifact, and SHA256."
    }
    if ([string]$receipt.handoff_status -cne "ok") {
        throw "Existing run receipt does not record a successful handoff."
    }
    return $true
}

function Assert-HandoffReceiptContent {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ReceiptPath,
        [Parameter(Mandatory = $true)]
        [object]$Handoff
    )

    $receipt = Read-JsonFile -Path $ReceiptPath
    if ([string]$receipt.bundle_id -cne $Handoff.BundleId -or
        [string]$receipt.bundle_sha256 -cne $Handoff.BundleSha256 -or
        [string]$receipt.agentic_researcher_commit -cne
        $Handoff.AgenticResearcherCommit -or
        [string]$receipt.media_manifest_sha256 -cne
        $Handoff.MediaManifestSha256) {
        throw "Existing handoff files no longer match their successful receipt."
    }
}

function Invoke-CurationAndExpansion {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RunRoot,
        [Parameter(Mandatory = $true)]
        [object]$Handoff,
        [Parameter(Mandatory = $true)]
        [string]$ArtifactHash
    )

    if (-not $AllowCommercialCuration) {
        throw "Stage '$Stage' requires the explicit -AllowCommercialCuration switch."
    }

    $curatedPath = Join-Path $RunRoot "curated_topics.json"
    $queuePath = Join-Path $RunRoot "research_queue.json"
    $curationRuns = Join-Path $RunRoot "curation"
    $receiptPath = Join-Path $RunRoot "curation.receipt.json"

    if (Test-Path -LiteralPath $receiptPath -PathType Leaf) {
        $receipt = Read-JsonFile -Path $receiptPath
        if ([string]$receipt.artifact_sha256 -cne $ArtifactHash -or
            [string]$receipt.status -cne "ok") {
            throw "Existing curation receipt does not match this artifact."
        }
        if (-not (Test-Path -LiteralPath $curatedPath -PathType Leaf) -or
            -not (Test-Path -LiteralPath $queuePath -PathType Leaf)) {
            throw "Curation receipt exists, but its required outputs are missing."
        }
        $curatedHash = (
            Get-FileHash -LiteralPath $curatedPath -Algorithm SHA256
        ).Hash.ToLowerInvariant()
        $queueHash = (
            Get-FileHash -LiteralPath $queuePath -Algorithm SHA256
        ).Hash.ToLowerInvariant()
        if ([string]$receipt.curated_topics_sha256 -cne $curatedHash -or
            [string]$receipt.research_queue_sha256 -cne $queueHash) {
            throw "Existing curation outputs no longer match their success receipt."
        }
        Write-ImportLog "Commercial curation and expansion already completed; skipping."
        return $queuePath
    }

    if ((Test-Path -LiteralPath $curatedPath) -or
        (Test-Path -LiteralPath $queuePath) -or
        (Test-Path -LiteralPath $curationRuns)) {
        throw "Partial curation output exists without a success receipt. Inspect it manually; automatic retry is disabled to avoid duplicate commercial spend."
    }

    $curateArguments = @(
        "-m", "agentic_researcher", "curate", $Handoff.Bundle,
        "--provider", "claude",
        "--provider", "codex",
        "--provider", "antigravity",
        "--runs-dir", $curationRuns,
        "--items-per-prompt", "12",
        "--max-input-chars", "24000",
        "--threshold", "2",
        "--output", $curatedPath
    )
    if (Test-Path -LiteralPath $Handoff.MediaRoot -PathType Container) {
        $curateArguments += @("--media-root", $Handoff.MediaRoot)
    }

    Write-ImportLog "Starting explicitly authorized Claude/Codex/Antigravity curation."
    Invoke-PythonModule -Arguments $curateArguments `
        -WorkingDirectory $script:AgenticRoot `
        -FailureLabel "Commercial curation"

    $curated = Read-JsonFile -Path $curatedPath
    if ($null -eq $curated.quality_gate -or
        $curated.quality_gate.all_required_curators_present -ne $true -or
        $curated.quality_gate.all_invoked_chunks_complete -ne $true -or
        $curated.quality_gate.degraded_consensus_allowed -ne $false) {
        throw "Curation quality gate did not confirm complete non-degraded 3-provider coverage."
    }
    $topics = @($curated.topics)
    $needsReviewCount = @(
        $topics | Where-Object { [string]$_.status -eq "needs_review" }
    ).Count

    Invoke-PythonModule -Arguments @(
        "-m", "agentic_researcher", "expand", $curatedPath,
        "--output", $queuePath
    ) -WorkingDirectory $script:AgenticRoot `
        -FailureLabel "Research queue expansion"
    $queue = Read-JsonFile -Path $queuePath
    if ($null -eq $queue.tasks) {
        throw "Expanded research queue does not contain a tasks array."
    }
    $curatedHash = (
        Get-FileHash -LiteralPath $curatedPath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    $queueHash = (
        Get-FileHash -LiteralPath $queuePath -Algorithm SHA256
    ).Hash.ToLowerInvariant()

    Write-JsonAtomic -Path $receiptPath -Value ([ordered]@{
            schema_version = "discord-math-local-curation-receipt/v1"
            status = "ok"
            artifact_sha256 = $ArtifactHash
            curated_topics_sha256 = $curatedHash
            research_queue_sha256 = $queueHash
            completed_at = Get-UtcTimestamp
            required_curators = @("antigravity", "claude", "codex")
            degraded_consensus_allowed = $false
            needs_review_count = $needsReviewCount
            research_task_count = @($queue.tasks).Count
        })
    Write-ImportLog (
        "Curation complete: {0} accepted queue tasks; {1} topic(s) remain needs_review." -f
        @($queue.tasks).Count, $needsReviewCount
    )
    return $queuePath
}

function Invoke-OpenWeightBatch {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RunRoot,
        [Parameter(Mandatory = $true)]
        [string]$QueuePath,
        [Parameter(Mandatory = $true)]
        [string]$ArtifactHash
    )

    if (-not $AllowOpenWeightBatch) {
        throw "Stage 'Full' requires the explicit -AllowOpenWeightBatch switch."
    }
    $queue = Read-JsonFile -Path $QueuePath
    if ($null -eq $queue.PSObject.Properties["tasks"] -or
        @($queue.tasks).Count -eq 0) {
        throw "Stage 'Full' requires a non-empty research queue."
    }
    $queueHash = (
        Get-FileHash -LiteralPath $QueuePath -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    $workRoot = Join-Path $RunRoot "research-projects"
    $statePath = Join-Path $RunRoot "batch-state.json"
    $receiptPath = Join-Path $RunRoot "batch.receipt.json"
    if (Test-Path -LiteralPath $receiptPath -PathType Leaf) {
        $existingReceipt = Read-JsonFile -Path $receiptPath
        if ([string]$existingReceipt.artifact_sha256 -cne $ArtifactHash -or
            [string]$existingReceipt.queue_sha256 -cne $queueHash) {
            throw "Existing batch receipt does not match this artifact and research queue."
        }
        if ([string]$existingReceipt.status -ceq "complete") {
            if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
                throw "Complete batch receipt exists, but batch-state.json is missing."
            }
            $existingState = Read-JsonFile -Path $statePath
            if ($null -eq $existingState.PSObject.Properties["summary"]) {
                throw "Complete batch state does not contain a summary."
            }
            if ($null -eq $existingState.PSObject.Properties["tasks"] -or
                @($existingState.tasks.PSObject.Properties).Count -ne
                @($queue.tasks).Count) {
                throw "Complete batch receipt does not cover the entire research queue."
            }
            foreach ($property in $existingState.summary.PSObject.Properties) {
                if ($property.Name -cne "success" -and
                    [int]$property.Value -gt 0) {
                    throw "Complete batch receipt conflicts with a non-success batch state."
                }
            }
            Write-ImportLog "Open-weight batch already completed successfully; skipping."
            return $statePath
        }
    }

    $dryRunRoot = Join-Path $RunRoot "batch-dry-run-projects"
    $dryRunState = Join-Path $RunRoot "batch-dry-run-state.json"
    if (-not (Test-Path -LiteralPath $dryRunState -PathType Leaf)) {
        Write-ImportLog "Validating the open-weight queue with a provider-free dry run."
        $dryArguments = @(
            "-m", "agentic_researcher", "run-batch", $QueuePath,
            "--work-root", $dryRunRoot,
            "--state", $dryRunState,
            "--provider", $OpenWeightProvider,
            "--dry-run"
        )
        if ($MaxTasks -gt 0) {
            $dryArguments += @("--max-tasks", [string]$MaxTasks)
        }
        Invoke-PythonModule -Arguments $dryArguments `
            -WorkingDirectory $script:AgenticRoot `
            -FailureLabel "Open-weight batch dry run"
    }

    Write-ImportLog "Starting explicitly authorized resumable open-weight batch."
    $batchArguments = @(
        "-m", "agentic_researcher", "run-batch", $QueuePath,
        "--work-root", $workRoot,
        "--state", $statePath,
        "--provider", $OpenWeightProvider,
        "--command", $OpenWeightCommand,
        "--max-attempts", [string]$MaxAttempts,
        "--timeout", [string]$ProviderTimeoutSeconds
    )
    if ($MaxTasks -gt 0) {
        $batchArguments += @("--max-tasks", [string]$MaxTasks)
    }
    Invoke-PythonModule -Arguments $batchArguments `
        -WorkingDirectory $script:AgenticRoot `
        -FailureLabel "Open-weight research batch"

    $state = Read-JsonFile -Path $statePath
    if ($null -eq $state.PSObject.Properties["summary"]) {
        throw "Open-weight batch state does not contain a summary."
    }
    $nonSuccess = [ordered]@{}
    $nonSuccessTotal = 0
    foreach ($property in $state.summary.PSObject.Properties) {
        $count = [int]$property.Value
        if ($property.Name -cne "success" -and $count -gt 0) {
            $nonSuccess[$property.Name] = $count
            $nonSuccessTotal += $count
        }
    }
    if ($null -eq $state.PSObject.Properties["tasks"]) {
        throw "Open-weight batch state does not contain task records."
    }
    $queueTaskCount = @($queue.tasks).Count
    $stateTaskCount = @($state.tasks.PSObject.Properties).Count
    if ($stateTaskCount -gt $queueTaskCount) {
        throw "Open-weight batch state contains tasks outside the research queue."
    }
    if ($stateTaskCount -lt $queueTaskCount) {
        $unseenCount = $queueTaskCount - $stateTaskCount
        $nonSuccess["queue_unseen"] = $unseenCount
        $nonSuccessTotal += $unseenCount
    }
    $batchStatus = if ($nonSuccessTotal -eq 0) {
        "complete"
    }
    else {
        "incomplete"
    }
    Write-JsonAtomic -Path $receiptPath -Value ([ordered]@{
            schema_version = "discord-math-local-batch-receipt/v1"
            status = $batchStatus
            artifact_sha256 = $ArtifactHash
            queue_sha256 = $queueHash
            completed_at = Get-UtcTimestamp
            provider = $OpenWeightProvider
            summary = $state.summary
            non_success = $nonSuccess
        })
    if ($nonSuccessTotal -gt 0) {
        throw "Open-weight batch is incomplete; see the protected batch receipt and state."
    }
    return $statePath
}

if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    throw "DataRoot cannot be empty."
}
if ([string]::IsNullOrWhiteSpace($IdentityFile)) {
    throw "IdentityFile cannot be empty."
}
if ($Repository -notmatch "^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$") {
    throw "Repository must use the owner/name form."
}
if ([string]::IsNullOrWhiteSpace($EncryptedArtifact) -xor
    [string]::IsNullOrWhiteSpace($RunId)) {
    throw "-EncryptedArtifact and -RunId must be supplied together for an offline import."
}
if (-not [string]::IsNullOrWhiteSpace($RunId) -and
    $RunId -notmatch "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$") {
    throw "RunId contains unsupported characters."
}
if ($SkipAclValidationForOfflineTest -and
    [string]::IsNullOrWhiteSpace($EncryptedArtifact)) {
    throw "-SkipAclValidationForOfflineTest is permitted only with -EncryptedArtifact."
}
if (($Stage -eq "Curate" -or $Stage -eq "Full") -and
    -not $AllowCommercialCuration) {
    throw "Stage '$Stage' requires -AllowCommercialCuration."
}
if ($Stage -eq "Full" -and -not $AllowOpenWeightBatch) {
    throw "Stage 'Full' requires -AllowOpenWeightBatch."
}

$script:DataRootFull = Resolve-FullPath -Path $DataRoot
$identityFull = Resolve-FullPath -Path $IdentityFile
if ([string]::IsNullOrWhiteSpace($AgenticResearcherRoot)) {
    $AgenticResearcherRoot = Split-Path -Parent $PSScriptRoot
}
$script:AgenticRoot = Resolve-FullPath -Path $AgenticResearcherRoot
if ([string]::IsNullOrWhiteSpace($DiscordRepositoryRoot)) {
    $workspaceRoot = Split-Path -Parent $script:AgenticRoot
    $DiscordRepositoryRoot = Join-Path $workspaceRoot "Discord-Mathematics-Early-University"
}
$script:DiscordRoot = Resolve-FullPath -Path $DiscordRepositoryRoot

Assert-PrivateStorage -Root $script:DataRootFull -Identity $identityFull `
    -SkipAcl:$SkipAclValidationForOfflineTest
Assert-RepositoryCheckout -AgenticRoot $script:AgenticRoot `
    -DiscordRoot $script:DiscordRoot

$script:LogsRoot = Join-Path $script:DataRootFull "logs"
$script:StateRoot = Join-Path $script:DataRootFull "state"
$script:DownloadsRoot = Join-Path $script:DataRootFull "downloads"
$script:RunsRoot = Join-Path $script:DataRootFull "runs"
$script:StagingRoot = Join-Path $script:DataRootFull "staging"
foreach ($directory in @(
        $script:LogsRoot,
        $script:StateRoot,
        $script:DownloadsRoot,
        $script:RunsRoot,
        $script:StagingRoot
    )) {
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        New-Item -ItemType Directory -Path $directory | Out-Null
    }
    Assert-NoReparsePoint -Path $directory -Label "Private automation directory"
    if (-not $SkipAclValidationForOfflineTest) {
        Assert-PrivateAcl -Path $directory `
            -Label "Private automation directory"
    }
}

$script:LogPath = Join-Path $script:LogsRoot (
    "import-{0}.log" -f [DateTime]::UtcNow.ToString("yyyyMMdd")
)
$script:PythonPath = Resolve-CommandPath -Requested $PythonExecutable `
    -CommandName "python"
$script:AgePath = Resolve-CommandPath -Requested $AgeExecutable `
    -CommandName "age" `
    -Fallbacks @(
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\age.exe")
    )

$offline = -not [string]::IsNullOrWhiteSpace($EncryptedArtifact)
if (-not $offline) {
    $script:GhPath = Resolve-CommandPath -Requested $GhExecutable `
        -CommandName "gh" `
        -Fallbacks @(
            (Join-Path $env:ProgramFiles "GitHub CLI\gh.exe")
        )
}

$mutexHasher = [System.Security.Cryptography.SHA256]::Create()
try {
    $mutexHashBytes = $mutexHasher.ComputeHash(
        [System.Text.Encoding]::UTF8.GetBytes($script:DataRootFull.ToLowerInvariant())
    )
}
finally {
    $mutexHasher.Dispose()
}
$mutexHash = ([BitConverter]::ToString($mutexHashBytes)).Replace("-", "").Substring(0, 20)
$mutex = New-Object -TypeName System.Threading.Mutex `
    -ArgumentList @($false, "Local\DiscordMathResearchImport-$mutexHash")
$mutexHeld = $false
$extractStaging = $null
try {
    try {
        $mutexHeld = $mutex.WaitOne(0)
    }
    catch [System.Threading.AbandonedMutexException] {
        $mutexHeld = $true
    }
    if (-not $mutexHeld) {
        throw "Another Discord Math import is already running for this DataRoot."
    }

    if ($offline) {
        $offlineFull = Resolve-FullPath -Path $EncryptedArtifact
        if (-not (Test-Path -LiteralPath $offlineFull -PathType Leaf) -or
            (Get-Item -LiteralPath $offlineFull).Length -le 0) {
            throw "Offline encrypted artifact is missing or empty."
        }
        $source = [PSCustomObject]@{
            Mode = "offline"
            RunId = [string]$RunId
            Attempt = 0
            RunCreatedAt = $null
            HeadSha = $null
            RunUrl = $null
            ArtifactId = "offline"
            ArtifactName = "offline"
            ArtifactSize = [long](Get-Item -LiteralPath $offlineFull).Length
            ArtifactDigest = $null
            ArtifactCreatedAt = $null
            ArtifactExpiresAt = $null
            LocalPath = $offlineFull
        }
        Write-ImportLog "Using explicitly supplied offline encrypted artifact."
    }
    else {
        Write-ImportLog "Selecting the newest successful workflow run with an eligible artifact."
        $source = Find-LatestArtifact
        $source | Add-Member -NotePropertyName Mode -NotePropertyValue "github"
        Write-ImportLog (
            "Selected GitHub run {0}, artifact {1}." -f
            $source.RunId, $source.ArtifactName
        )
    }

    $encryptedPath = Get-EncryptedArtifact -Source $source
    $artifactHash = (Get-FileHash -LiteralPath $encryptedPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $artifactFileSize = [long](Get-Item -LiteralPath $encryptedPath).Length
    $runKey = "run-{0}-artifact-{1}" -f $source.RunId, $source.ArtifactId
    $runRoot = Join-Path $script:RunsRoot $runKey
    $handoffReceiptPath = Join-Path $runRoot "handoff.receipt.json"

    if (Test-HandoffReceipt -ReceiptPath $handoffReceiptPath `
        -Source $source -ArtifactHash $artifactHash) {
        Write-ImportLog "Handoff already validated for the same run, artifact, and SHA256."
        $handoff = Assert-HandoffFiles -ExtractionRoot $runRoot
        Assert-HandoffReceiptContent -ReceiptPath $handoffReceiptPath `
            -Handoff $handoff
    }
    else {
        if (Test-Path -LiteralPath $runRoot) {
            throw "Run output exists without a matching successful handoff receipt: $runRoot"
        }
        $extractStaging = Join-Path $script:StagingRoot (
            "extract-{0}" -f [Guid]::NewGuid().ToString("N")
        )
        Write-ImportLog "Decrypting and safely extracting the artifact as a stream."
        Invoke-PythonModule -Arguments @(
            "-m", "agentic_researcher.secure_artifacts",
            $encryptedPath, $extractStaging,
            "--identity", $identityFull,
            "--age-executable", $script:AgePath
        ) -WorkingDirectory $script:AgenticRoot `
            -FailureLabel "Encrypted artifact extraction"

        $stagedHandoff = Assert-HandoffFiles -ExtractionRoot $extractStaging
        Invoke-ContractSmoke -Handoff $stagedHandoff
        Write-JsonAtomic `
            -Path (Join-Path $extractStaging "handoff.receipt.json") `
            -Value ([ordered]@{
                schema_version = "discord-math-local-handoff-receipt/v1"
                run_id = [string]$source.RunId
                run_attempt = [int]$source.Attempt
                run_created_at = $source.RunCreatedAt
                source_head_sha = $source.HeadSha
                run_url = $source.RunUrl
                artifact_id = [string]$source.ArtifactId
                artifact_name = [string]$source.ArtifactName
                artifact_api_size_bytes = [long]$source.ArtifactSize
                artifact_api_digest = $source.ArtifactDigest
                artifact_file_size_bytes = $artifactFileSize
                artifact_created_at = $source.ArtifactCreatedAt
                artifact_expires_at = $source.ArtifactExpiresAt
                artifact_sha256 = $artifactHash
                handoff_status = "ok"
                validated_at = Get-UtcTimestamp
                bundle = "discord_exports/ingest_bundle.json"
                bundle_id = $stagedHandoff.BundleId
                bundle_sha256 = $stagedHandoff.BundleSha256
                agentic_researcher_commit =
                    $stagedHandoff.AgenticResearcherCommit
                media_manifest = "discord_exports/curation_media/media_manifest.json"
                media_manifest_sha256 =
                    $stagedHandoff.MediaManifestSha256
            })
        [System.IO.Directory]::Move($extractStaging, $runRoot)
        $extractStaging = $null
        $handoff = Assert-HandoffFiles -ExtractionRoot $runRoot
        Write-ImportLog "Encrypted artifact passed safe extraction and contract validation."
    }

    $completedStage = "Handoff"
    $queuePath = $null
    $batchStatePath = $null
    if ($Stage -eq "Curate" -or $Stage -eq "Full") {
        $queuePath = Invoke-CurationAndExpansion -RunRoot $runRoot `
            -Handoff $handoff -ArtifactHash $artifactHash
        $completedStage = "Curate"
    }
    if ($Stage -eq "Full") {
        $batchStatePath = Invoke-OpenWeightBatch -RunRoot $runRoot `
            -QueuePath $queuePath -ArtifactHash $artifactHash
        $completedStage = "Full"
    }

    $latest = [ordered]@{
        schema_version = "discord-math-local-latest/v1"
        status = "ok"
        requested_stage = $Stage
        completed_stage = $completedStage
        run_id = [string]$source.RunId
        artifact_id = [string]$source.ArtifactId
        artifact_sha256 = $artifactHash
        bundle_id = $handoff.BundleId
        bundle_sha256 = $handoff.BundleSha256
        agentic_researcher_commit = $handoff.AgenticResearcherCommit
        media_manifest_sha256 = $handoff.MediaManifestSha256
        completed_at = Get-UtcTimestamp
        run_root = $runRoot
        bundle = $handoff.Bundle
        research_queue = $queuePath
        batch_state = $batchStatePath
    }
    Write-JsonAtomic -Path (Join-Path $script:StateRoot "latest.json") -Value $latest
    Write-ImportLog "Discord Math import completed at stage '$completedStage'."
    $latest | ConvertTo-Json -Depth 8
}
finally {
    if ($null -ne $extractStaging -and
        (Test-Path -LiteralPath $extractStaging)) {
        Remove-PrivateTree -Path $extractStaging
    }
    if ($mutexHeld) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
