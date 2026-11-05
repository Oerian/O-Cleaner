<#
  fix-encoding.ps1 -- normalize text file encoding and line endings.

  WHY THIS EXISTS
  This project has been bitten more than once by the same class of bug:
    * cmd.exe mis-parses .bat files that use LF line endings
      (it swallows the next line into the previous echo)
    * PowerShell 5.1 reads files WITHOUT a UTF-8 BOM as GBK on
      Chinese Windows, which turns Chinese text into mojibake and
      breaks the parser with confusing "missing closing brace" errors

  Editing a .ps1 with an editor that drops the BOM breaks the script.
  This file is intentionally written in pure ASCII so it can still run
  when everything else is broken.

  USAGE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\fix-encoding.ps1
    powershell ... -File tools\fix-encoding.ps1 -Check    # report only, no writes
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [string]$Root = ""
)

$ErrorActionPreference = 'Stop'
if (-not $Root) {
    $Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
}

# extension -> required encoding + line ending
#
# BOM 策略：只有 .ps1 需要 BOM。
#   * PowerShell 5.1 会把没有 BOM 的文件当 GBK 读，中文变乱码，并报出
#     难以理解的 "missing closing brace"（这个项目多次遇到）
#   * Python 3 / 浏览器 / JSON 解析器都能正确处理无 BOM 的 UTF-8
#   * 给 .py 加 BOM 虽不影响运行，但没有必要
$rules = @{
    '.bat'  = @{ Encoding = 'ascii';   Eol = 'crlf' }
    '.cmd'  = @{ Encoding = 'ascii';   Eol = 'crlf' }
    '.ps1'  = @{ Encoding = 'utf8bom'; Eol = 'crlf' }
    '.py'   = @{ Encoding = 'utf8';    Eol = 'crlf' }
    '.json' = @{ Encoding = 'utf8';    Eol = 'crlf' }
    '.html' = @{ Encoding = 'utf8';    Eol = 'crlf' }
    '.md'   = @{ Encoding = 'utf8';    Eol = 'crlf' }
    '.yml'  = @{ Encoding = 'utf8';    Eol = 'crlf' }
    '.yaml' = @{ Encoding = 'utf8';    Eol = 'crlf' }
    '.js'   = @{ Encoding = 'utf8';    Eol = 'crlf' }
}

$utf8Bom = New-Object System.Text.UTF8Encoding($true)
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$ascii = New-Object System.Text.ASCIIEncoding

function Test-HasBom {
    param([byte[]]$Bytes)
    return ($Bytes.Length -ge 3 -and $Bytes[0] -eq 0xEF -and $Bytes[1] -eq 0xBB -and $Bytes[2] -eq 0xBF)
}

$fixed = 0
$problems = @()

# Skip generated / vendored directories
# _legacy：前身版本归档（dsh-cleaner/、clean-c-drive*.ps1、legacy 备份 zip）。
# 它们是历史文物，必须保持原样，不参与本仓库的编码契约。该目录已 gitignore，
# CI 从 git 签出，本来也看不到它 —— 这条跳过规则只为本地开发时不产生噪音。
$skipDirs = @('__pycache__', 'dist', '.git', 'node_modules', '_legacy')

Get-ChildItem -LiteralPath $Root -Recurse -File | ForEach-Object {
    $file = $_
    foreach ($d in $skipDirs) {
        if ($file.FullName -like "*\$d\*") { return }
    }
    $ext = $file.Extension.ToLower()
    if (-not $rules.ContainsKey($ext)) { return }

    $rule = $rules[$ext]
    $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
    $hasBom = Test-HasBom -Bytes $bytes
    $text = [System.IO.File]::ReadAllText($file.FullName)

    $crlf = ([regex]::Matches($text, "`r`n")).Count
    $loneLf = ([regex]::Matches($text, "(?<!`r)`n")).Count
    # 双重 CR（CR CR LF）：此前 Python 脚本 newline='' 写入与本项目归一化
    # 组合出的「不动点」—— 旧归一化对它原样保留，CPython 解析器又容忍
    # 它，导致污染静默积累；唯一暴露点是反斜杠续行（ocleaner/core.py
    # 迁移时实测断过一次）。检测 + 归一一起补上。
    $doubleCr = ([regex]::Matches($text, "`r`r")).Count
    $nonAscii = 0
    foreach ($b in $bytes) { if ($b -gt 127) { $nonAscii++ } }

    $need = @()
    if ($loneLf -gt 0) { $need += "LF->CRLF ($loneLf)" }
    if ($doubleCr -gt 0) { $need += "CR->CRLF ($doubleCr)" }
    if ($rule.Encoding -eq 'utf8bom' -and -not $hasBom) { $need += 'add BOM' }
    if ($rule.Encoding -eq 'utf8' -and $hasBom) { $need += 'remove BOM' }
    if ($rule.Encoding -eq 'ascii' -and $nonAscii -gt 0) { $need += "non-ASCII ($nonAscii bytes)" }

    if ($need.Count -eq 0) { return }

    $rel = $file.FullName.Substring($Root.Length + 1)
    $msg = "{0}  ->  {1}" -f $rel, ($need -join ', ')
    $problems += $msg

    if ($Check) {
        Write-Host ("  [NEEDS FIX] " + $msg) -ForegroundColor Yellow
        return
    }

    # normalize line endings: strip ALL CR first then re-add CRLF so the
    # pass is idempotent (a CR-CR-LF sequence is truly collapsed, not kept)
    $body = ($text -replace "`r", "") -replace "`n", "`r`n"
    if ($body.Length -gt 0 -and $body[0] -eq [char]0xFEFF) { $body = $body.Substring(1) }

    if ($rule.Encoding -eq 'ascii') {
        [System.IO.File]::WriteAllText($file.FullName, $body, $ascii)
    } elseif ($rule.Encoding -eq 'utf8bom') {
        [System.IO.File]::WriteAllText($file.FullName, $body, $utf8Bom)
    } else {
        [System.IO.File]::WriteAllText($file.FullName, $body, $utf8NoBom)
    }
    Write-Host ("  [FIXED] " + $msg) -ForegroundColor Green
    $script:fixed++
}

Write-Host ""
if ($problems.Count -eq 0) {
    Write-Host "All text files already conform." -ForegroundColor Green
} elseif ($Check) {
    Write-Host ("{0} file(s) need fixing. Re-run without -Check to fix." -f $problems.Count) -ForegroundColor Yellow
    exit 1
} else {
    Write-Host ("Fixed {0} file(s)." -f $fixed) -ForegroundColor Green
}
exit 0
