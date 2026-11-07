"""O-Cleaner 的 PowerShell 脚本模板 —— 从 app.py 拆出的纯常量模块。

这里只有字符串常量，没有任何逻辑：模板的渲染、占位符替换与子进程管理
仍在 app.py 的 run_ps 与各调用点。独立成模块便于集中审阅与维护
这批仓库里最大的静态块。

改动约定：
- 模板里 __xxx__ 形式的占位符由 Python 侧 .replace() 注入，渲染结果会
  直接交给 PowerShell 5.1 执行 —— 花括号必须配平（linter 第 [7/8] 节与
  selftest 阶段七都会把关）。
- 脚本文本经 UTF-8 管道传入 PowerShell（见 PS_UTF8_PREAMBLE），
  中文注释与中文字面量因此不乱码；不要改回系统默认代码页。
- 本文件遵循仓库编码契约：UTF-8 无 BOM + CRLF。
"""

# PowerShell 5.1 通过管道输出时使用控制台的 ANSI 代码页（中文系统为 GBK），
# 而 Python 侧按 UTF-8 解码 —— 结果中文名会变成乱码（软件 -> ����）。
# 强制让子进程把管道输出设为 UTF-8，与控制台 chcp 无关。
PS_UTF8_PREAMBLE = ("$OutputEncoding=[System.Text.Encoding]::UTF8;"
                    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;")



# 回收站：按盘符定位。用 try/catch 把权限问题收敛成 size=-1，
# 而不是让 PowerShell 以非零退出码结束并丢掉 stdout。
PS_RECYCLE = r"""
$ErrorActionPreference='SilentlyContinue'
$drive='__DRIVE__'
$items=@()
$total=0
$count=0
$denied=$false
if ($drive) {
    $root = $drive + ':\$Recycle.Bin'
    if (Test-Path -LiteralPath $root) {
        $ErrorActionPreference='Stop'
        try {
            $all=@(Get-ChildItem -LiteralPath $root -Recurse -Force -File -ErrorAction Stop)
            $count=$all.Count
            $total=[int64](($all | Measure-Object -Property Length -Sum).Sum)
            $items=@($all | Where-Object { $_.Name -notlike '$I*' -and $_.Length -gt 1MB } |
                     Sort-Object Length -Descending |
                     Select-Object -First 40 @{n='name';e={$_.Name}},@{n='size';e={[int64]$_.Length}},@{n='path';e={$_.FullName}})
        } catch {
            $denied=$true
        }
        $ErrorActionPreference='SilentlyContinue'
    }
}
[pscustomobject]@{size=$total; files=$count; items=$items; denied=$denied} |
    ConvertTo-Json -Compress -Depth 4
"""

PS_TREE_WALK = r"""
$ErrorActionPreference='SilentlyContinue'
$root = '__ROOT__'
$minChild = __MINCHILD__
$ratio = __RATIO__
$floor = __FLOOR__
$maxDepth = __MAXDEPTH__
$maxPerDir = __MAXPERDIR__
$maxNodes = __MAXNODES__
$maxDescents = __MAXDESCENTS__
# 进度文件：Python 侧每 0.5 秒读一次，把「预算已用」换算成百分比。
# 为空串表示不需要上报（例如从命令行直接调用本脚本）。
$progFile = '__PROGFILE__'

$script:out = New-Object System.Collections.ArrayList
$script:budget = $maxNodes
$script:sizeCache = @{}
$script:tick = 0

# 目录体积按需测量并缓存 —— 同一目录只量一次
function Size-Of([string]$p) {
    if ($script:sizeCache.ContainsKey($p)) { return $script:sizeCache[$p] }
    $m = Get-ChildItem -LiteralPath $p -Recurse -Force -File -ErrorAction SilentlyContinue |
         Measure-Object -Property Length -Sum
    $v = [int64]$m.Sum
    $script:sizeCache[$p] = $v
    return $v
}

function Min-Descend([int64]$parentSize) {
    $d = [int64]($parentSize * $ratio)
    if ($d -lt $floor) { $d = [int64]$floor }
    if ($d -gt 4000000000) { $d = [int64]4000000000 }
    return $d
}

function Walk([string]$p, [int]$depth, [string]$parentKey, [int64]$selfSize) {
    if ($depth -gt $maxDepth -or $script:budget -le 0) { return }
    $minDescend = Min-Descend $selfSize

    $ErrorActionPreference='Stop'
    try { $all = @(Get-ChildItem -LiteralPath $p -Force -ErrorAction Stop) }
    catch { $all = @() }
    $ErrorActionPreference='SilentlyContinue'

    $kids = @()
    foreach ($d in ($all | Where-Object { $_.PSIsContainer })) {
        if ($d.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { continue }
        $sz = Size-Of $d.FullName
        if ($sz -lt $minChild) { continue }
        $kids += [pscustomobject]@{ type='dir'; name=$d.Name; path=$d.FullName
            size=$sz; modified=$d.LastWriteTime.ToString('yyyy-MM-dd') }
    }
    foreach ($f in ($all | Where-Object { -not $_.PSIsContainer })) {
        if ($f.Length -lt $minChild) { continue }
        $kids += [pscustomobject]@{ type='file'; name=$f.Name; path=$f.FullName
            size=[int64]$f.Length; modified=$f.LastWriteTime.ToString('yyyy-MM-dd') }
    }
    $kids = @($kids | Sort-Object -Property size -Descending | Select-Object -First $maxPerDir)

    # 只在最粗的几条分支上继续深入，控制总耗时
    $descended = 0
    foreach ($k in $kids) {
        if ($script:budget -le 0) { break }
        $script:budget--
        # 每 25 个节点报一次进度。写文件比 Set-Content 循环便宜得多，
        # 但也不能每个节点都写 —— 递归本身才是瓶颈，别让它被 I/O 拖慢。
        $script:tick++
        if ($progFile -ne '' -and ($script:tick % 25) -eq 0) {
            try {
                [System.IO.File]::WriteAllText($progFile, "$($maxNodes - $script:budget)/$maxNodes")
            } catch { }
        }
        $key = $k.path.ToLower()
        $hasKids = $false
        if ($k.type -eq 'dir' -and $k.size -ge $minDescend -and $depth -lt $maxDepth `
            -and $descended -lt $maxDescents -and $script:budget -gt 0) {
            $descended++
            $hasKids = $true
        }
        [void]$script:out.Add([pscustomobject]@{ parent=$parentKey; name=$k.name
            path=$k.path; type=$k.type; size=$k.size; depth=$depth
            modified=$k.modified; hasChildren=$hasKids })
        if ($hasKids) { Walk $k.path ($depth + 1) $key $k.size }
    }
}

$total = 0
$ErrorActionPreference='Stop'
try { $tops = @(Get-ChildItem -LiteralPath $root -Force -Directory -ErrorAction Stop) }
catch { $tops = @() }
$ErrorActionPreference='SilentlyContinue'

$topList = @()
foreach ($d in $tops) {
    if ($d.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { continue }
    $sz = Size-Of $d.FullName
    $topList += [pscustomobject]@{ name=$d.Name; path=$d.FullName; size=$sz
        modified=$d.LastWriteTime.ToString('yyyy-MM-dd') }
    $total += $sz
}
$topList = @($topList | Sort-Object -Property size -Descending)

foreach ($t in $topList) {
    if ($script:budget -le 0) { break }
    $key = $t.path.ToLower()
    $script:budget--
    $hasKids = ($t.size -ge (Min-Descend $total) -and $script:budget -gt 0)
    [void]$script:out.Add([pscustomobject]@{ parent=$root.ToLower(); name=$t.name
        path=$t.path; type='dir'; size=$t.size; depth=1
        modified=$t.modified; hasChildren=$hasKids })
    if ($hasKids) { Walk $t.path 2 $key $t.size }
}

# 盘符根目录下的大文件
$rootFiles = @()
$ErrorActionPreference='Stop'
try { $rf = @(Get-ChildItem -LiteralPath $root -Force -File -ErrorAction Stop) }
catch { $rf = @() }
$ErrorActionPreference='SilentlyContinue'
foreach ($f in $rf) {
    if ($f.Length -lt $minChild) { continue }
    $rootFiles += [pscustomobject]@{ name=$f.Name; path=$f.FullName
        size=[int64]$f.Length; modified=$f.LastWriteTime.ToString('yyyy-MM-dd') }
}

[pscustomobject]@{
    root=$root.ToLower()
    nodes=$script:out
    rootfiles=$rootFiles
    total=[int64]$total
} | ConvertTo-Json -Compress -Depth 4
"""

PS_BIG_FILES = r"""
$ErrorActionPreference='SilentlyContinue'
$root = '__ROOT__'
$floor = __FLOOR__
$progFile = '__PROGFILE__'

# 只收集超过体积门槛的文件：绝大多数文件远小于门槛，内存与耗时都可控。
# 大文件排行关心的是视频 / 镜像 / 安装包这个量级的单文件。
$script:found = 0
$out = New-Object System.Collections.Generic.List[object]
Get-ChildItem -LiteralPath $root -Recurse -Force -File -ErrorAction SilentlyContinue |
  ForEach-Object {
    if ($_.Length -gt $floor) {
      $mtime = ''
      try { $mtime = $_.LastWriteTime.ToString('yyyy-MM-dd HH:mm') } catch {}
      $out.Add([pscustomobject]@{ p = $_.FullName; s = [int64]$_.Length; m = $mtime })
      $script:found++
      if ($progFile -and ($script:found % 200 -eq 0)) {
        try { [IO.File]::WriteAllText($progFile, [string]$script:found) } catch {}
      }
    }
  }
if ($progFile) { try { [IO.File]::WriteAllText($progFile, [string]$script:found) } catch {} }
$out | ConvertTo-Json -Compress -Depth 2
"""


PS_STEAM = r"""
$ErrorActionPreference='SilentlyContinue'
$roots = @()
foreach ($k in @('HKCU:\Software\Valve\Steam', 'HKLM:\SOFTWARE\WOW6432Node\Valve\Steam')) {
    $p = (Get-ItemProperty -Path $k -ErrorAction SilentlyContinue).SteamPath
    if ($p) { $roots += $p }
}
foreach ($p in @('C:\Steam','D:\Steam','D:\steam','E:\Steam','E:\steam',
                 (Join-Path ${env:ProgramFiles(x86)} 'Steam'),
                 (Join-Path $env:ProgramFiles 'Steam'))) {
    if ($p) { $roots += $p }
}

$acfs = @()
foreach ($root in ($roots | Select-Object -Unique)) {
    if (-not (Test-Path -LiteralPath $root)) { continue }
    $libPaths = @($root)
    $vdf = Join-Path $root 'steamapps\libraryfolders.vdf'
    if (Test-Path -LiteralPath $vdf) {
        $txt = Get-Content -LiteralPath $vdf -Raw -ErrorAction SilentlyContinue
        foreach ($m in [regex]::Matches($txt, '"path"\s+"([^"]+)"')) {
            $libPaths += ($m.Groups[1].Value -replace '\\\\', '\')
        }
    }
    foreach ($lp in ($libPaths | Select-Object -Unique)) {
        $sa = Join-Path $lp 'steamapps'
        if (-not (Test-Path -LiteralPath $sa)) { continue }
        foreach ($f in (Get-ChildItem -LiteralPath $sa -Filter 'appmanifest_*.acf' -File -ErrorAction SilentlyContinue)) {
            $acfs += [pscustomobject]@{ library=$lp; path=$f.FullName }
        }
    }
}
[pscustomobject]@{
    roots = @($roots | Select-Object -Unique)
    manifests = @($acfs | ForEach-Object { $_.library + '|' + $_.path })
} | ConvertTo-Json -Compress -Depth 4
"""

# 残留目录体积：单独一次（路径数量少，代价可控）
PS_STEAM_ORPHAN_SIZE = r"""
$ErrorActionPreference='SilentlyContinue'
$paths = @()
if (Test-Path -LiteralPath '__LIST__') {
    $paths = @(Get-Content -LiteralPath '__LIST__' -Encoding UTF8 | Where-Object { $_ })
}
$out = @()
foreach ($p in $paths) {
    if (-not (Test-Path -LiteralPath $p)) { continue }
    $m = Get-ChildItem -LiteralPath $p -Recurse -Force -File -ErrorAction SilentlyContinue |
         Measure-Object -Property Length -Sum
    $out += ('{{"path":"{0}","size":{1}}}' -f ($p -replace '\\', '/'), [int64]$m.Sum)
}
# 手工拼 JSON 数组，不用 ConvertTo-Json：
# 它在数组**只有一个元素**时会退化成序列化对象自身属性
# （Name/FullName/Length/Mode…），实测因此拿到一堆 path 为 null 的条目、体积全变 0。
# 路径统一转成正斜杠，彻底避开反斜杠转义问题。
'[' + ($out -join ',') + ']'
"""

PS_LOCK_HOLDERS = r"""
$targets = @(__TARGETS__)
$rows = New-Object System.Collections.ArrayList
foreach ($pr in (Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
    # 跳过扫描者自己：本脚本把目标路径当作字面量写在命令行里，
    # 不排除的话 powershell.exe 一定会「命令行里含这个完整路径」命中自己，
    # 既制造假候选、又把真正的候选挤到后面。
    if ($pr.ProcessId -eq $PID) { continue }
    $exe = $pr.ExecutablePath
    $cmd = $pr.CommandLine
    if ([string]::IsNullOrEmpty($exe) -and [string]::IsNullOrEmpty($cmd)) { continue }
    foreach ($t in $targets) {
        $dir = [System.IO.Path]::GetDirectoryName($t)
        $nm  = [System.IO.Path]::GetFileName($t)
        $why = ''
        $strong = $false
        if (-not [string]::IsNullOrEmpty($exe) -and -not [string]::IsNullOrEmpty($dir)) {
            $pfx = $dir.ToLower() + [System.IO.Path]::DirectorySeparatorChar
            if ($exe.ToLower().StartsWith($pfx)) {
                $strong = $true; $why = '程序本体就在这个目录下'
            }
        }
        if (-not $strong -and -not [string]::IsNullOrEmpty($cmd)) {
            $cl = $cmd.ToLower()
            if ($cl.Contains($t.ToLower())) { $strong = $true; $why = '命令行里含这个完整路径' }
            elseif ($nm.Length -ge 4 -and $cl.Contains($nm.ToLower())) { $why = '命令行里含同名文件' }
        }
        if ($why -ne '') {
            [void]$rows.Add([pscustomobject]@{
                target = $t; pid = [int]$pr.ProcessId; name = $pr.Name
                exe = $exe; why = $why; strong = [bool]$strong })
        }
    }
}
if ($rows.Count -eq 0) { '[]' }
else { $rows | Select-Object -First __LIMIT__ | ConvertTo-Json -Compress -Depth 3 }
"""

