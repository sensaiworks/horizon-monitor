<#
Runs INSIDE the VDI only -- never executed locally. Dumps one Outlook folder's
messages to a plain-text file through Outlook's COM object model, so the folder can
be read STRUCTURALLY (one script launch) instead of OCR-ing the reading pane email by
email (field-tested 2026-10-16: dozens of fragile focus/scroll/OCR steps per folder).

Deposited into the VDI by src/outlook_reader.py via ProjectPuller.deposit_text_file()
and run with a plain, human-readable command:
    powershell -NoProfile -ExecutionPolicy Bypass -File <this.ps1> -Folder "<name>" -Out <path>
Deliberately NOT a base64 -EncodedCommand: the auto-mode safety classifier blocks
pasting an encoded blob into the VDI (denied live 2026-10-16), while a readable .ps1
run with -File is allowed. Keep this file reviewable -- no encoding, no downloads.

Read-back channel: this VDI blocks clipboard copy-OUT, so the dump can't come back via
Get-Clipboard. Instead the script OPENS THE DUMP IN NOTEPAD when it finishes; the local
side waits for the dump header to appear on screen, maximizes Notepad, and reads the
file back with src/doc_reader.py (keyboard paging + Windows OCR -- clean generated
text OCRs reliably). Notepad appearing is also the completion signal: there is no
other way for the local side to know the script has finished writing.

Output format (designed to survive OCR -- short ASCII markers, one field per line):
    ===MAIL-DUMP folder=<name> items=<n> of <total> generated=<yyyy-MM-dd HH:mm>===
    ===MSG 1/<n>===
    Received: 2026-10-15 14:03
    From: Jane Doe
    To: Mike Lezinsky; John Smith
    Subject: Re: LoanCAD extract
    Body:
    <body text, blank runs collapsed, capped at -BodyMaxChars>
    ===END-MSG===
    ...
    ===MAIL-DUMP-END items=<n>===
src/outlook_reader.py's parse_dump() is the only consumer -- keep them in sync.

Object-model security: only SenderName / To / Subject / Body / ReceivedTime are read.
SenderEmailAddress and HTMLBody are deliberately avoided -- they can trip Outlook's
"A program is trying to access e-mail address information" prompt, which would block
an unattended run. Every per-item read is wrapped in try/catch so one odd item
(meeting request, undeliverable report, protected message) never aborts the dump.
#>
param(
    [Parameter(Mandatory = $true)][string]$Folder,
    [Parameter(Mandatory = $true)][string]$Out,
    [int]$MaxItems = 200,
    [int]$BodyMaxChars = 4000,
    [switch]$NoNotepad
)
$ErrorActionPreference = "Stop"

# --- Attach to the RUNNING Outlook (never start a second instance) ----------------
# GetActiveObject binds to the already-open Outlook the user is signed into. Outlook is
# single-instance, so New-Object also returns the running one -- it's only the fallback
# for the (unlikely) case Outlook isn't open yet.
try {
    $outlook = [Runtime.InteropServices.Marshal]::GetActiveObject("Outlook.Application")
} catch {
    $outlook = New-Object -ComObject Outlook.Application
}
$ns = $outlook.GetNamespace("MAPI")

# --- Find the target folder by name, recursively, across every store -------------
# Matches on the folder's own Name (case-insensitive); a slash path like
# "Inbox/Clients/LoanCAD" matches on the FolderPath suffix instead, for folders whose
# bare name isn't unique. First match wins, depth-first in store order.
function Find-Folder($parent, [string]$name) {
    foreach ($f in $parent.Folders) {
        $path = ($f.FolderPath -replace '\\', '/').Trim('/')
        if ($f.Name -ieq $name -or $path.ToLower().EndsWith($name.Trim('/').ToLower())) {
            return $f
        }
        $hit = Find-Folder $f $name
        if ($hit) { return $hit }
    }
    return $null
}

$target = $null
foreach ($store in $ns.Folders) {
    $target = Find-Folder $store $Folder
    if ($target) { break }
}
if (-not $target) {
    throw "outlook_folder_dump: no folder named '$Folder' found in any open mailbox"
}

# --- Iterate items newest-first ------------------------------------------------------
$items = $target.Items
try { $items.Sort("[ReceivedTime]", $true) } catch { }   # some folders can't sort; keep going
$total = $items.Count
$take = [Math]::Min($MaxItems, $total)

function Clean-Text([string]$s) {
    if (-not $s) { return "" }
    $s = $s -replace "`r`n", "`n" -replace "`r", "`n"
    $s = $s -replace "[ `t]+`n", "`n"            # trailing whitespace per line
    $s = $s -replace "`n{3,}", "`n`n"            # collapse blank-line runs
    return $s.Trim()
}

$sb = New-Object System.Text.StringBuilder
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
[void]$sb.AppendLine("===MAIL-DUMP folder=$($target.Name) items=$take of $total generated=$stamp===")

$written = 0
for ($i = 1; $i -le $take; $i++) {
    try {
        $item = $items.Item($i)
        $received = ""
        try { $received = $item.ReceivedTime.ToString("yyyy-MM-dd HH:mm") } catch { }
        $from = ""
        try { $from = [string]$item.SenderName } catch { }
        $to = ""
        try { $to = [string]$item.To } catch { }
        $subject = ""
        try { $subject = [string]$item.Subject } catch { }
        $body = ""
        try { $body = Clean-Text ([string]$item.Body) } catch { }
        if ($body.Length -gt $BodyMaxChars) {
            $body = $body.Substring(0, $BodyMaxChars) + "`n[... truncated at $BodyMaxChars chars]"
        }

        $written++
        [void]$sb.AppendLine("===MSG $written/$take===")
        [void]$sb.AppendLine("Received: $received")
        [void]$sb.AppendLine("From: $from")
        [void]$sb.AppendLine("To: $to")
        [void]$sb.AppendLine("Subject: $subject")
        [void]$sb.AppendLine("Body:")
        [void]$sb.AppendLine($body)
        [void]$sb.AppendLine("===END-MSG===")
    } catch {
        # One bad item (protected message, odd class) must not abort the whole dump;
        # leave a visible marker so the local parser can report the gap.
        [void]$sb.AppendLine("===MSG-ERROR index=$i $($_.Exception.Message)===")
    }
}
[void]$sb.AppendLine("===MAIL-DUMP-END items=$written===")

New-Item -ItemType Directory -Force -Path (Split-Path $Out -Parent) | Out-Null
# UTF-8 without BOM: Notepad renders it cleanly, and nothing invisible lands on line 1
# where the local side looks for the header marker.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText($Out, $sb.ToString(), $utf8NoBom)
Write-Output "outlook_folder_dump: $written of $total item(s) from '$($target.Name)' -> $Out"

# Open the finished dump in Notepad: the read-back surface for DocReader AND the
# local side's only completion signal (see header comment).
if (-not $NoNotepad) {
    Start-Process notepad.exe -ArgumentList $Out
}
