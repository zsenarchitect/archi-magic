<#
.SYNOPSIS
  Render PDF pages to PNG files with the PDF engine built into Windows (Windows.Data.Pdf).
  No Python, no poppler, no admin rights.

.DESCRIPTION
  render-pdf-pages.ps1 -Pdf <file.pdf> -OutDir <folder> [-Pages "1-3,5"] [-Dpi 150]
  render-pdf-pages.ps1 -Check

  Use it when the Read tool cannot show a PDF page (for example "pdftoppm is not
  installed") and there is no Python. Then read the PNG files.

  Output, one line for each page:
    PDF <n> page(s)
    PAGE <page> <width>x<height> <png path>
  The PNG name is <pdf name>_p<page>.png. An existing PNG with that name is replaced.
  The PDF stays unchanged.

  -Pages   Page list, for example "1", "2-4" or "1,3,5-6". Default: all pages.
  -Dpi     Pixels per inch, 30 to 600. Default 150. A large sheet (for example
           36 x 48 in) at 150 dpi is 5400 x 7200 px. Use a lower value for a first look.
  -Check   Only test that Windows.Data.Pdf can load. Prints "WINRT_PDF ok".

  Exit codes: 0 ok, 1 PDF not found or cannot open (damaged or password protected),
  2 bad -Pages or -Dpi value, 3 Windows.Data.Pdf is not available (PowerShell 7,
  or Windows older than 10).
  Windows PowerShell 5.1 only (powershell.exe). ASCII only.
#>
param(
    [string]$Pdf,
    [string]$OutDir,
    [string]$Pages = '',
    [int]$Dpi = 150,
    [switch]$Check
)
$ErrorActionPreference = 'Stop'

function Write-Fail([int]$code, [string]$msg) {
    [Console]::Error.WriteLine($msg)
    exit $code
}

# "1,3,5-6" -> sorted unique page numbers (1-based). Returns $null for bad text.
function ConvertTo-PageList([string]$text, [int]$count) {
    if ([string]::IsNullOrWhiteSpace($text)) { return @(1..$count) }
    $list = New-Object System.Collections.Generic.List[int]
    foreach ($part in $text.Split(',')) {
        $p = $part.Trim()
        $m = [regex]::Match($p, '^(\d+)(?:\s*-\s*(\d+))?$')
        if (-not $m.Success) { return $null }
        $a = [int]$m.Groups[1].Value
        $b = $a
        if ($m.Groups[2].Success) { $b = [int]$m.Groups[2].Value }
        if ($a -lt 1 -or $b -lt $a) { return $null }
        for ($i = $a; $i -le $b; $i++) { if (-not $list.Contains($i)) { $list.Add($i) } }
    }
    $list.Sort()
    return @($list)
}

if ($PSVersionTable.PSEdition -eq 'Core') {
    Write-Fail 3 'WINRT_PDF missing: run this script with Windows PowerShell 5.1 (powershell.exe), not PowerShell 7.'
}

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
    $null = [Windows.Data.Pdf.PdfDocument, Windows.Data.Pdf, ContentType = WindowsRuntime]
    $null = [Windows.Data.Pdf.PdfPageRenderOptions, Windows.Data.Pdf, ContentType = WindowsRuntime]
    $null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapEncoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
} catch {
    Write-Fail 3 ('WINRT_PDF missing: Windows.Data.Pdf cannot load (' + $_.Exception.Message + '). Ask the user to export the pages as PNG.')
}

# WinRT async calls return IAsyncOperation<T> or IAsyncAction. Wait for them with AsTask().
$asTaskGeneric = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
$asTaskAction = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and -not $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction'
} | Select-Object -First 1
if (-not $asTaskGeneric -or -not $asTaskAction) { Write-Fail 3 'WINRT_PDF missing: no AsTask bridge in System.Runtime.WindowsRuntime.' }

function Wait-Operation($op, [Type]$resultType) {
    $task = $asTaskGeneric.MakeGenericMethod($resultType).Invoke($null, @($op))
    [void]$task.Wait(-1)
    return $task.Result
}
function Wait-Action($op) {
    $task = $asTaskAction.Invoke($null, @($op))
    [void]$task.Wait(-1)
}

if ($Check) {
    'WINRT_PDF ok'
    exit 0
}

if ([string]::IsNullOrWhiteSpace($Pdf)) { Write-Fail 2 'Give -Pdf <file.pdf>.' }
if ([string]::IsNullOrWhiteSpace($OutDir)) { Write-Fail 2 'Give -OutDir <folder>.' }
if ($Dpi -lt 30 -or $Dpi -gt 600) { Write-Fail 2 "-Dpi must be 30 to 600, not $Dpi." }
if (-not (Test-Path -LiteralPath $Pdf -PathType Leaf)) { Write-Fail 1 "PDF not found: $Pdf" }

$pdfFull = (Resolve-Path -LiteralPath $Pdf).ProviderPath
try {
    $file = Wait-Operation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($pdfFull)) ([Windows.Storage.StorageFile])
    $doc = Wait-Operation ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($file)) ([Windows.Data.Pdf.PdfDocument])
} catch {
    $e = $_.Exception
    while ($e.InnerException) { $e = $e.InnerException }
    Write-Fail 1 ('Cannot open the PDF (damaged or password protected?): ' + $e.Message)
}

$count = [int]$doc.PageCount
if ($count -lt 1) { Write-Fail 1 'The PDF has no pages.' }
"PDF $count page(s)"
$list = ConvertTo-PageList $Pages $count
if ($null -eq $list) { Write-Fail 2 "Bad -Pages value '$Pages'. Use for example 1 or 2-4 or 1,3,5-6." }
$over = @($list | Where-Object { $_ -gt $count })
if ($over.Count -gt 0) { Write-Fail 2 ("Page " + ($over -join ',') + " is not in the PDF (it has $count page(s)).") }

if (-not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
$outFull = (Resolve-Path -LiteralPath $OutDir).ProviderPath
$base = [IO.Path]::GetFileNameWithoutExtension($pdfFull)

foreach ($n in $list) {
    $page = $doc.GetPage([uint32]($n - 1))
    $stream = $null
    $fs = $null
    try {
        # Page size is in DIPs (1/96 in).
        $w = [Math]::Max(1, [int][Math]::Round($page.Size.Width * $Dpi / 96.0))
        $h = [Math]::Max(1, [int][Math]::Round($page.Size.Height * $Dpi / 96.0))
        $opt = New-Object Windows.Data.Pdf.PdfPageRenderOptions
        $opt.DestinationWidth = [uint32]$w
        $opt.DestinationHeight = [uint32]$h
        $opt.BitmapEncoderId = [Windows.Graphics.Imaging.BitmapEncoder]::PngEncoderId
        $stream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
        Wait-Action ($page.RenderToStreamAsync($stream, $opt))
        $png = Join-Path $outFull ("{0}_p{1}.png" -f $base, $n)
        $netStream = [System.IO.WindowsRuntimeStreamExtensions]::AsStream($stream)
        [void]$netStream.Seek(0, [IO.SeekOrigin]::Begin)
        $fs = [IO.File]::Create($png)
        $netStream.CopyTo($fs)
        "PAGE $n ${w}x${h} $png"
    } finally {
        if ($fs) { $fs.Dispose() }
        if ($stream) { $stream.Dispose() }
        $page.Dispose()
    }
}
exit 0
