<#
Runs INSIDE the VDI only -- never executed locally. Zips a project (minus junk dirs
and oversized files), base64-encodes the zip, and writes the base64 as chunked,
hash-verified plain-text files ("part_NNN.txt") into a staging directory. Those
files are what gets printed to a local PDF and decoded by src/pull_payload.py --
see that module and DECISIONS.md for why: base64 is a plain ASCII stream, so printing
it and extracting it back with `pdftotext -raw` has no indentation/line-wrap
ambiguity, unlike printing source code directly. Fidelity is proven by SHA-256, not
inferred from "it compiles".

Deliberately excludes the same junk directories src/remote_tree.py's
DEFAULT_EXCLUDE_DIRS already defines (kept in sync by hand -- see that module's
comment) so this and the older per-file pull agree on what "the project" means.

Invoked two ways by src/pull_payload.py's PayloadPuller, chosen by whether the
finished command fits under the remote command-line length ceiling:
  1. -EncodedCommand: everything from the #BODY-START# line onward, base64-encoded,
     with $ProjectRoot/$OutDir/etc. assigned as plain variables (spliced in by
     _build_encoded_command) INSTEAD of this file's param() block -- a script run
     via -EncodedCommand has no separate argument list to bind to param().
  2. -File (fallback): this whole file, deposited as-is via
     ProjectPuller.deposit_text_file(), invoked with normal -ProjectRoot/-OutDir/...
     named arguments -- param() binds those the standard way.
Writes three kinds of output into $OutDir (the staging dir):
  * part_NNN.txt -- the chunked base64 payload, one print job each.
  * manifest.txt -- written LAST (so its existence means every export file
    is fully flushed), marker-delimited, with a header carrying total_sha256/
    zip_bytes/kept/skipped/report so `--resume` can validate already-printed PDFs
    against THIS batch before reprinting anything.
  * _contents.txt -- NOT in $OutDir but staged INSIDE the zip, so it comes back
    byte-exact with the payload instead of needing a print job of its own. Lists
    every kept file with size+SHA-256, every file skipped for exceeding
    MaxFileBytes, and a per-rule tally of files dropped by ExcludeDirs. This is what
    makes COMPLETENESS checkable locally -- the zip hash proves the bytes are right,
    it cannot prove nothing was silently left out.

Keep the marker line below exact and on its own line -- _build_encoded_command
splits the file on it verbatim.
#>
param(
    [Parameter(Mandatory = $true)][string]$ProjectRoot,
    [Parameter(Mandatory = $true)][string]$OutDir,
    [string]$ProjectName = "",
    [string[]]$ExcludeDirs = @(
        ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv", "env",
        "dist", "build", "bin", "obj", ".vs", ".idea", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", ".tox", "target", ".gradle", ".next", ".parcel-cache",
        "python", "site-packages", "Lib"
    ),
    [long]$MaxFileBytes = 5MB,
    [int]$ChunkChars = 4000,
    [int]$MaxCharsPerFile = 200000
)
#BODY-START#
$ErrorActionPreference = "Stop"

$ProjectRoot = $ProjectRoot.TrimEnd('\')
if (-not $ProjectName) { $ProjectName = Split-Path $ProjectRoot -Leaf }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$stage = Join-Path $OutDir "stage"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

# --- Discover + stage files (exclude junk dirs, skip oversized files) ----------
$excludeSet = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
foreach ($d in $ExcludeDirs) { [void]$excludeSet.Add($d) }

$skipped = New-Object System.Collections.Generic.List[string]
$kept = New-Object System.Collections.Generic.List[string]
$keptMeta = New-Object System.Collections.Generic.List[string]
# Files dropped by an ExcludeDirs rule, counted per rule that matched. Byte-exactness
# says nothing about COMPLETENESS: a project directory legitimately named `bin`/
# `build`/`dist`/`obj` is silently erased by the default rules, and without this
# tally the local side has no way to notice. Counted, reported, never guessed at.
$excludedByDir = @{}

Get-ChildItem -Path $ProjectRoot -Recurse -File -Force | ForEach-Object {
    $rel = $_.FullName.Substring($ProjectRoot.Length).TrimStart('\')
    $parts = $rel -split '\\'
    $hitDir = $null
    for ($i = 0; $i -lt $parts.Length - 1; $i++) {
        if ($excludeSet.Contains($parts[$i])) { $hitDir = $parts[$i]; break }
    }
    if ($hitDir) {
        if ($excludedByDir.ContainsKey($hitDir)) { $excludedByDir[$hitDir]++ }
        else { $excludedByDir[$hitDir] = 1 }
        return
    }
    if ($_.Length -gt $MaxFileBytes) {
        $skipped.Add("$rel`t$($_.Length)`tover MaxFileBytes=$MaxFileBytes")
        return
    }
    $destPath = Join-Path $stage $rel
    New-Item -ItemType Directory -Force -Path (Split-Path $destPath -Parent) | Out-Null
    Copy-Item -Path $_.FullName -Destination $destPath -Force
    $kept.Add($rel)
    $h = (Get-FileHash -Path $_.FullName -Algorithm SHA256).Hash.ToLower()
    $keptMeta.Add("$rel`t$($_.Length)`t$h")
}

if ($kept.Count -eq 0) {
    throw "project_archive: no files matched under $ProjectRoot (check ExcludeDirs / MaxFileBytes)"
}

# --- Completeness report, written INTO the staged tree so it rides back INSIDE the
#     zip ------------------------------------------------------------------------
# Deliberately in the zip rather than as a separate staging-dir file: anything left
# beside the export files needs its own print job to come back, and an earlier
# version's `export_skipped.txt` was written remotely and then never retrieved by
# anything -- a file skipped for size vanished with zero local signal. In the zip it
# is byte-exact, SHA-256-covered, and impossible to forget to fetch.
$reportName = "_contents.txt"
$collision = 0
while (Test-Path (Join-Path $stage $reportName)) {
    $collision++
    $reportName = "_contents_$collision.txt"
}
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$rep = New-Object System.Text.StringBuilder
[void]$rep.AppendLine("===ARCHIVE-CONTENTS===")
[void]$rep.AppendLine("project=$ProjectName")
[void]$rep.AppendLine("root=$ProjectRoot")
[void]$rep.AppendLine("max_file_bytes=$MaxFileBytes")
[void]$rep.AppendLine("exclude_dirs=" + ($ExcludeDirs -join ','))
[void]$rep.AppendLine("kept=$($kept.Count)")
[void]$rep.AppendLine("skipped=$($skipped.Count)")
[void]$rep.AppendLine("report_name=$reportName")
[void]$rep.AppendLine("---KEPT relpath<TAB>bytes<TAB>sha256---")
foreach ($line in $keptMeta) { [void]$rep.AppendLine($line) }
[void]$rep.AppendLine("---SKIPPED relpath<TAB>bytes<TAB>reason---")
foreach ($line in $skipped) { [void]$rep.AppendLine($line) }
[void]$rep.AppendLine("---EXCLUDED-DIRS name<TAB>files---")
foreach ($k in ($excludedByDir.Keys | Sort-Object)) {
    [void]$rep.AppendLine("$k`t$($excludedByDir[$k])")
}
[void]$rep.AppendLine("===ARCHIVE-CONTENTS-END===")
[IO.File]::WriteAllText((Join-Path $stage $reportName), $rep.ToString(), $utf8NoBom)

# --- Zip the staged tree (staged first so zip entries are clean relative paths) -
$zipPath = Join-Path $OutDir "export.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zipPath -CompressionLevel Optimal

# --- Hash + base64 the zip -------------------------------------------------------
$zipBytes = [IO.File]::ReadAllBytes($zipPath)
$sha256 = [Security.Cryptography.SHA256]::Create()
$totalHash = -join ($sha256.ComputeHash($zipBytes) | ForEach-Object { $_.ToString("x2") })
$b64 = [Convert]::ToBase64String($zipBytes)

# --- Chunk the base64 string, hashing each chunk's raw characters ---------------
$chunks = New-Object System.Collections.Generic.List[object]
$pos = 0
$idx = 1
while ($pos -lt $b64.Length) {
    $len = [Math]::Min($ChunkChars, $b64.Length - $pos)
    $text = $b64.Substring($pos, $len)
    $chunkHash = -join ($sha256.ComputeHash([Text.Encoding]::ASCII.GetBytes($text)) | ForEach-Object { $_.ToString("x2") })
    $chunks.Add([pscustomobject]@{ Index = $idx; Sha256 = $chunkHash; Text = $text })
    $pos += $len
    $idx++
}
$totalChunks = $chunks.Count

# --- Group chunks into part_NNN.txt files bounded by MaxCharsPerFile ----------
$chunksPerFile = [Math]::Max(1, [Math]::Floor($MaxCharsPerFile / $ChunkChars))
$fileGroups = New-Object System.Collections.Generic.List[object]
for ($start = 0; $start -lt $totalChunks; $start += $chunksPerFile) {
    $end = [Math]::Min($start + $chunksPerFile, $totalChunks) - 1
    $fileGroups.Add(@($start, $end))
}
$numFiles = $fileGroups.Count
$pad = [Math]::Max(3, "$numFiles".Length)
$exportNames = New-Object System.Collections.Generic.List[string]

for ($f = 0; $f -lt $numFiles; $f++) {
    $startIdx, $endIdx = $fileGroups[$f]
    $firstChunk = $chunks[$startIdx].Index
    $lastChunk = $chunks[$endIdx].Index
    $fname = "part_" + ("$($f + 1)".PadLeft($pad, '0')) + ".txt"
    $fpath = Join-Path $OutDir $fname
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.AppendLine("===ARCHIVE-BEGIN project=$ProjectName file=$($f + 1)/$numFiles chunks=$firstChunk-$lastChunk/$totalChunks total_sha256=$totalHash zip_bytes=$($zipBytes.Length)===")
    for ($c = $startIdx; $c -le $endIdx; $c++) {
        $chunk = $chunks[$c]
        [void]$sb.AppendLine("---CHUNK $($chunk.Index) sha256=$($chunk.Sha256)---")
        [void]$sb.AppendLine($chunk.Text)
    }
    [void]$sb.AppendLine("===ARCHIVE-END===")
    [IO.File]::WriteAllText($fpath, $sb.ToString(), [Text.Encoding]::ASCII)
    $exportNames.Add($fname)
}

Remove-Item $stage -Recurse -Force
Remove-Item $zipPath -Force

# Written LAST, deliberately: src/pull_payload.py polls for the EXISTENCE of this
# one file rather than listing the staging directory, so it never observes a
# partially-written part_NNN.txt mid-write -- by the time this file exists,
# every export file above has already been fully flushed to disk.
#
# The header line carries total_sha256/zip_bytes so `--resume` can decide whether
# export PDFs already on local disk belong to THIS export batch before reprinting
# anything, and the BEGIN/END markers let the local parser reject a stale Notepad
# tab printed in place of the manifest (the same guard parse_export_text applies).
$manifestPath = Join-Path $OutDir "manifest.txt"
$mf = New-Object System.Text.StringBuilder
[void]$mf.AppendLine("===ARCHIVE-MANIFEST project=$ProjectName files=$numFiles chunks=$totalChunks total_sha256=$totalHash zip_bytes=$($zipBytes.Length) kept=$($kept.Count) skipped=$($skipped.Count) report=$reportName===")
foreach ($exportName in $exportNames) { [void]$mf.AppendLine($exportName) }
[void]$mf.AppendLine("===ARCHIVE-MANIFEST-END===")
[IO.File]::WriteAllText($manifestPath, $mf.ToString(), [Text.Encoding]::ASCII)

Write-Output "project_archive: $ProjectName -- $($kept.Count) files, $totalChunks chunks, $numFiles part file(s), $($skipped.Count) skipped"
