<#
.SYNOPSIS
  Stage 7.8B-1: the operator-side clean-tree preflight of an evaluation manifest (DESIGN_78B1_SIP_EVALUATION.md section 5).

.DESCRIPTION
  Asks git (read-only commands, --no-optional-locks) whether the working tree is exactly the checked-out commit, and refuses otherwise:
  nothing staged, nothing unstaged, no untracked file, no unmerged path, no index entry hidden from git status (assume-unchanged,
  skip-worktree), no ignored .py file outside .venv, and an empty `git status --porcelain`. Product code never starts a process (the Stage
  3.9 rule), so these git checks live here, outside it.

  -Out FILE       writes the CLEAN record (commit, tree, every check NONE) that `python -m swing_eval.manifest build --preflight FILE`
                  requires; the manifest CLI re-checks the record against this checkout. FILE must be new, outside the repository and not a
                  database name. Nothing is written when a check fails.
  -Manifest FILE  the same checks, then the manifest's code_commit must be the checked-out commit and its code_tree that commit's tree
                  (git rev-parse). Writes nothing. Run it before approving a manifest and again before the evaluation.

  The record is a procedural control: git's answers on this machine, not signed.
  Exit codes: 0 CLEAN (with -Manifest: and the manifest's code matches), 2 REFUSED (every finding printed).

.EXAMPLE
  .\swing_eval\preflight.ps1 -Out "C:\Data Analyst Py\DA IDX\evidence\stage-78B\preflight.json"
.EXAMPLE
  .\swing_eval\preflight.ps1 -Manifest "C:\Data Analyst Py\DA IDX\evidence\stage-78B\manifest.json"
#>
[CmdletBinding()]
param(
    [string]$Out = "",
    [string]$Manifest = "",
    [string]$Repo = "",
    [string]$Git = ""
)
$ErrorActionPreference = "Stop"
$Checks = @("staged", "unstaged", "untracked", "unmerged", "hidden_by_index_flags", "ignored_python", "status")   # = swing_eval.codetree.PREFLIGHT_CHECKS
$DbSuffixes = @(".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".db-journal")                                   # = research_data.DB_SUFFIXES

function Find-Git([string]$Given) {
    if ($Given) { return $Given }
    $c = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    foreach ($p in @("$env:ProgramFiles\Git\cmd\git.exe", "${env:ProgramFiles(x86)}\Git\cmd\git.exe", "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe")) {
        if ($p -and (Test-Path -LiteralPath $p)) { return $p }
    }
    throw "git was not found (pass -Git)"
}

function Invoke-Git([string[]]$GitArgs) {
    $ErrorActionPreference = "Continue"                     # a git warning on stderr is not a failure; the exit code is
    $lines = & $script:GitExe --no-optional-locks -C $script:Top @GitArgs
    if ($LASTEXITCODE -ne 0) { throw "git $($GitArgs -join ' ') failed (exit $LASTEXITCODE)" }
    return @($lines | Where-Object { $_ -ne "" })
}

function Refuse([string[]]$Why) {
    Write-Output "PREFLIGHT REFUSED"
    foreach ($w in $Why) { Write-Output "  $w" }
    exit 2
}

try {
    if (($Out -and $Manifest) -or (-not $Out -and -not $Manifest)) { Refuse @("pass exactly one of -Out FILE or -Manifest FILE") }
    $GitExe = Find-Git $Git
    if (-not $Repo) { $Repo = Split-Path -Parent $PSScriptRoot }
    $Top = $Repo
    $Top = @(Invoke-Git @("rev-parse", "--show-toplevel"))[0]
    $commit = @(Invoke-Git @("rev-parse", "--verify", "HEAD"))[0]
    $tree = @(Invoke-Git @("rev-parse", "--verify", "HEAD:"))[0]
    $findings = @()
    $result = [ordered]@{}
    $found = [ordered]@{
        "staged" = @(Invoke-Git @("diff", "--cached", "--name-only", "--no-renames", "--ignore-submodules=none", "HEAD", "--"))
        "unstaged" = @(Invoke-Git @("diff", "--name-only", "--no-renames", "--ignore-submodules=none", "--"))
        "untracked" = @(Invoke-Git @("ls-files", "--others", "--exclude-standard"))
        "unmerged" = @(Invoke-Git @("ls-files", "--unmerged"))
        "hidden_by_index_flags" = @(Invoke-Git @("ls-files", "-v") | Where-Object { $_ -cmatch '^([a-z]|S) ' })
        "ignored_python" = @(Invoke-Git @("ls-files", "--others", "--ignored", "--exclude-standard", "--", "*.py") | Where-Object { $_ -notmatch '^\.venv/' })
        "status" = @(Invoke-Git @("status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"))
    }
    foreach ($name in $Checks) {
        $lines = @($found[$name])
        if ($lines.Count -eq 0) { $result[$name] = "NONE" } else {
            $result[$name] = "FOUND"
            $findings += @($lines | Select-Object -First 20 | ForEach-Object { "${name}: $_" })
        }
    }
    if ($commit -cnotmatch '^[0-9a-f]{40}$' -or $tree -cnotmatch '^[0-9a-f]{40}$') { $findings += "the commit or its tree is not a sha1: $commit $tree" }
    if ($findings.Count -gt 0) { Refuse $findings }

    if ($Manifest) {
        $m = Get-Content -LiteralPath $Manifest -Raw -Encoding UTF8 | ConvertFrom-Json
        $mc = [string]$m.strategy.code_commit
        $mt = [string]$m.strategy.code_tree
        $why = @()
        if ($mc -cnotmatch '^[0-9a-f]{40}$') { $why += "the manifest's code_commit is not a sha1: '$mc'" } else {
            if ($mc -ne $commit) { $why += "the manifest's code_commit $mc is not the checked-out commit $commit" }
            $ct = @(Invoke-Git @("rev-parse", "--verify", ($mc + "^{tree}")))[0]
            if ($mt -ne $ct) { $why += "the manifest's code_tree $mt is not its commit's tree $ct" }
        }
        if ($why.Count -gt 0) { Refuse $why }
        Write-Output "PREFLIGHT CLEAN: the manifest's code is the checked-out commit $commit, tree $tree (manifest sha256 $($m.manifest_sha256))"
        exit 0
    }

    $target = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Out)
    $topWin = [System.IO.Path]::GetFullPath(($Top -replace '/', '\')).TrimEnd('\')
    $leaf = (Split-Path -Leaf $target).ToLowerInvariant()
    if ($target.StartsWith($topWin + "\", [System.StringComparison]::OrdinalIgnoreCase)) { Refuse @("$target is inside the repository (R17)") }
    foreach ($s in $DbSuffixes) { if ($leaf.EndsWith($s)) { Refuse @("${leaf}: a database path (R16: JSON evidence only)") } }
    if (Test-Path -LiteralPath $target) { Refuse @("$target exists (a preflight record is never overwritten)") }
    $branch = @(Invoke-Git @("branch", "--show-current"))
    $record = [ordered]@{
        "kind" = "sip_eval_preflight"
        "version" = 1
        "status" = "CLEAN"
        "tool" = "swing_eval/preflight.ps1"
        "repository" = $Top
        "commit" = $commit
        "tree" = $tree
        "branch" = $(if ($branch.Count -gt 0) { $branch[0] } else { "(detached)" })
        "checked_at" = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        "git" = @(Invoke-Git @("--version"))[0]
        "checks" = $result
        "note" = "procedural control: git's answers on the operator's machine, not signed"
    }
    $parent = Split-Path -Parent $target
    if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    [System.IO.File]::WriteAllText($target, (($record | ConvertTo-Json -Depth 4) + "`n"), (New-Object System.Text.UTF8Encoding($false)))
    Write-Output "PREFLIGHT CLEAN: commit $commit, tree $tree -> $target"
    exit 0
} catch {
    Write-Output "PREFLIGHT REFUSED"
    Write-Output "  $($_.Exception.Message)"
    exit 2
}
