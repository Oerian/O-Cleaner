# O-Cleaner Usage Guide

> This guide is the English counterpart of [docs/USAGE.md](USAGE.md) (Chinese).

Complete usage instructions. For the project's positioning, core capabilities, and quick start, see the [README](../README.md).

> This guide is the English counterpart of [docs/USAGE.md](USAGE.md) (Chinese).

---

## 1. Quick Start

1. Make sure Python 3 is installed (running `python --version` in a terminal should print a version number)
2. Double-click **`启动 O-Cleaner.bat`**
3. Your browser will open `http://127.0.0.1:8770/` automatically

> **That black command-line window is the service itself — do not close it during cleanup.**
> Closing the window = stopping the service, and the web page will stop connecting. Close it once all cleanup is done.

### Portable Version (No Python Needed)

`O-Cleaner-v<version>-win64-embed.zip` is a portable build that **embeds the official Python runtime**: after extracting, just double-click `启动 O-Cleaner.bat`. No need to install or configure Python on that machine.

- Portable use: history and configuration are written inside the extracted folder, so you can put the whole folder in any writable directory; do not place it in locations requiring administrator rights such as Program Files.
- The `python/` directory is the embedded runtime — keep it intact; do not move or trim it on its own.
- On first run, SmartScreen may show "Windows protected your PC" — this program is not code-signed; click "More info" → "Run anyway" to proceed. Uninstalling is just deleting the folder; no registry entries are left behind.

To clean system directories (Windows Update cache, system logs, etc.), run **as administrator**: search `cmd` in the Start menu → right-click "Run as administrator" → run

```
cd /d "your O-Cleaner directory"
启动 O-Cleaner.bat
```

Command-line arguments:

| Argument | Effect |
|---|---|
| `--port 9000` | Specify the port (default 8770; if occupied, it automatically tries the next one) |
| `--no-browser` | Do not open the browser automatically on startup |
| `--data-dir D:\oc-data` | Redirect the data directory: history, configuration, scheduled tasks, and other runtime data go to the specified location (the `OC_DATA_DIR` environment variable also works) |
| `--scan-only` | Only print a text scan report; do not start the service |
| `--scan-only --drive D` | Scan the specified drive |

### The Data Directory and USB-Drive Portability

History, disk trends, scheduled tasks, `config\` (including custom explanations and resource packs) — **all runtime data** lives in the data directory, whose location is shown in the "Environment Self-Check" (运行环境自检) panel in the right sidebar:

- **Default: same directory as the program** — fully portable. Put the whole O-Cleaner folder on a USB drive or copy it to another machine and the data travels with it; unplug and it's gone, leaving no trace on the system.
- **Program directory is read-only, or you want to back up data separately**: use `--data-dir <path>` or the `OC_DATA_DIR` environment variable to redirect runtime data to any writable location (USB drive, another disk, etc.). The startup log and the self-check panel will honestly note "redirected".
- **Migration / backup**: copy the data directory as a whole; after switching machines, put it back in the corresponding location (or point to it with the same `--data-dir`) for a complete restore.

Self-tests (300+ assertions, including real deletions and end-to-end HTTP; the count varies by machine):

```
python -X utf8 tools\selftest.py
python -X utf8 tools\lint_rules.py --strict      # rules and resource pack gate
python -X utf8 tools\_lint_selftest.py           # verifies the previous gate can actually catch defects
```

There is also a browser-based check, which requires the service to be running first (uses the system's existing Edge/Chrome, no extra install):

```
node tools\cdp_check.js http://127.0.0.1:8770
```

---

## 2. Using the Interface

On startup it automatically scans the current drive (default: a data drive; the last-used drive takes priority; with only one drive it is C:). **Nothing is deleted by default — you must select items yourself.**

### Layout and Navigation

The interface has three columns: **left sidebar** (drive selection, presets, custom directories, disk trends),
**main area** (cleanup item cards, full-disk usage ranking, Steam game library),
**right sidebar** (collapsible panels: self-check, resource packs, AI, statistics, rules, history, etc.).

Two functional pages live as permanent tabs at the top: **Regular Cleanup** and **Steam Game Library**.
Click a tab to switch; the current page is highlighted — no need to hunt for entries through layers of buttons.
The right sidebar has many panels (a dozen or more); each collapsible panel can be expanded/collapsed by clicking its title.
At the top of the sidebar there is a **panel directory** listing all panel titles; clicking any entry expands and scrolls to that panel —
when you can't find a feature, check the directory bar first instead of expanding panels one by one.
Pressing `Esc` goes back one level (breakdown views, drill-down levels, etc.); while a scan is running it will tell you to wait.

The **disk trend** in the left sidebar is a 30-day step line of this drive's free space; each bend is one cleanup.
Hover near a bend to see that cleanup's time, freed amount, and free space afterwards.

The disk overview (capacity, usage, cleanable/keep/unrecognized shares) appears in exactly one place — the scan overview page;
Steam library info lives on the Steam Game Library page and the card to its left; each has its own job, without redundant stacking.

### Themes

Next to the top navigation are three small buttons: "Light / Dark / Auto" — daytime mode, night mode, follow the system.
The preference is stored in the browser's local storage; "Auto" switches in real time with the OS light/dark setting;
the trend chart canvas takes its colors from theme variables and redraws immediately after switching.

### Interface Language

Next to the theme buttons are three small buttons: `A / 中 / EN`. `A` follows the browser language
(if the browser is not in a Chinese locale, English is shown); `中` and `EN` force a specific language.
The preference is stored in the browser's local storage and survives a refresh. Chinese is the source-code default;
the interface skeleton, dynamic texts (badge verdicts, hints, panel titles, sort and view labels,
confirmation dialogs, disk overview and trend chart labels) and knowledge-base long texts (the "What is it / How to handle it / Tips" sections of built-in rules and
resource pack explanations, risk and confidence labels,
category words) are all bilingual; in the English interface, scan results automatically show English descriptions;
switching languages takes effect immediately and re-fetches data.

### Custom Explanations and Verdicts

For rows with a target annotation, clicking "Details" opens a dialog containing a "Custom Explanation" (自定义释义) button: you can rewrite the item's
four description sections ("What is it / Can it be rebuilt / Deletion impact / Why keep it"), and you can also change the verdict
to "cleanable" or "not cleanable". Modified items show the new description in their details;
"Select All" and manual selection follow the new verdict if it was changed.

- Priority: **your edits > resource packs > built-in descriptions**; "Restore Default" reverts in one click.
- Regular cleanup items take effect by type (shared across drives); "auto-discovered" directories take effect by specific path.
- Changing a high-risk item to "cleanable" does not lower its risk level: during cleanup it is still handled as high-risk
  (deleted to the Recycle Bin by default).
- Data is stored in `config\custom-overrides.json` (a local file, not distributed with the repo);
  it travels along when you copy the whole package to another computer.

### Choosing a Drive

On the top right are drive cards showing each drive's free space and usage ratio. Click to switch.
Each drive has a different target list:

- **System drive (C:)** includes system items like `Windows\Temp`, the update cache, Prefetch, Minidump, etc.
- **Data drives (D:/E:)** only list things that actually exist on that drive; empty entries like `D:\Windows\Temp` are never listed
- **Custom directory**: type any path in the toolbar (e.g. `E:\GameCache`) and it will scan it for known junk folders

### Cleanup Scope

The "Cleanup Scope" (清理范围) option in the toolbar decides what the list includes:

| Option | Meaning |
|---|---|
| Current drive + user-level caches | Besides drive-specific items, this also includes user-level caches that are independent of the drive letter (uv / pip / browser caches etc., located under `%LOCALAPPDATA%`) |
| Current-drive items only | Only items directly related to the selected drive, e.g. `D:\Temp`, `D:\$Recycle.Bin` |

When a data drive (D/E…) is selected, "current-drive items only" is the default: `%LOCALAPPDATA%`
always expands to the user directory on the system drive, so such items have nothing to do with the selected drive and appearing in the D-drive list only invites
the question "why does drive D have browser caches"; they exist in the C-drive list too.

(Earlier versions did not filter this; selecting drive D would show "D drive's pip cache" with a path like `C:\Users\…`.
This has been fixed, and a self-test assertion pins the invariant: target paths on non-system drives must reside on that drive.)

### Auto-Discovery (the Key That Makes Data Drives Useful)

Predefined targets only cover common system locations (`Windows\Temp`, the Recycle Bin, etc.), but the bulk of disk usage
is often the **long tail of personal software**. So the regular scan also actively looks at the top level of the current drive
for directories "recognized as cleanable by the knowledge base" — such items carry an orange "Auto-discovered" badge.

What auto-discovery covers depends on what is actually installed on that drive, e.g. cloud-drive caches, music app caches,
driver packs, Office deployment caches, downloader caches, etc. — these directories have no entries in the predefined list,
and without active search they would never appear.

Safety constraints of auto-discovery:

- Only candidates judged **cleanable** with non-low confidence by the knowledge base are accepted; unrecognized ones are always skipped (better to under-report than guess)
- The inside of the Recycle Bin is excluded (`$Recycle.Bin\S-1-5-...`-style SID directories have no explanatory value), as are
  `ProgramData`, `AppData`, `Windows` and similar locations
- **`Program Files` / `Program Files (x86)` are excluded** — that's program bodies, not caches
- **Directories that "contain an installed program" are excluded** — see the next item
- For same-named parent/child directories only the outermost one is kept, avoiding `steam` and `steam\appcache` appearing together
- When a parent directory is itself a cleanable candidate, its children are no longer recommended one by one
  (otherwise the clip-draft directories under a downloads directory would be recommended as 8 separate cleanup items,
  and what's inside is the user's own work)
- At most 12 items, at most two levels deep — an overly long list only paralyses people

#### How "this directory contains a program" is detected

Auto-discovery matches the knowledge base by directory name, and the knowledge base's keywords were written for cache directories,
so install directories can be matched by mistake. Known examples:

- A directory whose name contains the short word `ps` matches the Photoshop rule, while such directories can also be
  install directories of Adobe software
- `C:\Program Files (x86)\Adobe` and `C:\Program Files\Microsoft OneDrive` were once judged cleanable too

So an additional check was added that looks only at generic traits, not names (looking at names is exactly what caused the problem):

| Criterion | Explanation |
|---|---|
| `Uninstall*.exe` / `unins*.exe` at the top level | Uninstallers left by the installer |
| More than one "version-number-shaped" subdirectory | Year-based (e.g. `Photoshop 2024`) or four-segment version numbers (e.g. `WPS Office\12.1.0.x`) |

Two criteria deliberately not used:

- "Number of top-level .exe files" — a downloads directory often has several `setup.exe` at its top level,
  so counting them would misjudge the downloads directory as a program install and drop a useful candidate
- "An uninstaller exists in a subdirectory" — install directories of already-installed software under a downloads directory all carry uninstallers, which would misfire the same way

Uninstallers only count when they appear at the top level. The principle: better to under-report than to recommend "uninstalling software" as "cleaning a cache".

### One-Click Presets

On the right is "One-Click Presets"; the most used is **Safe Cleanup**: it only selects items that "rebuild themselves after deletion",
skipping the Recycle Bin, shader caches, and items requiring administrator rights.

| Preset | Effect |
|---|---|
| Safe Cleanup | Caches and temporary files; the safest choice |
| Deep Cleanup | Additionally selects GPU shader caches, thumbnails, crash dumps, system logs |
| Developer Cleanup | Only cleans uv / pip package caches |
| Recycle Bin Only | Only selects the Recycle Bin; you can expand it and pick individual large files |
| Clear All | Clears all selections |

### Look Before You Delete

The **`Details`** button on the right of each row in the usage ranking opens the full description: **identified as / category / risk level /
identification basis**, plus **what it is / cleanup advice / full path**.
For example, the pip cache's "why keep it" says "you rely on offline installs, or you frequently reinstall the same set of dependencies".
Rows judged "cleanable" have a checkbox on the left; checking one adds it to the bottom "Selected / Estimated free space" counter.

### Automatic Full-Disk Scan (One Click; Shows the Largest Items by Default)

Clicking **`Full-Disk Scan This Drive` (全盘扫描本盘)** in the toolbar **starts the scan automatically** — no second click needed.
When the scan finishes, the view by default directly shows **"Largest Items on the Disk" (全盘最大项目)**, listing the big space consumers in descending order of actual size;
you can also switch to "Current Level" (本层目录), "Files Only" (只看文件) or "All Cleanable" (全部可清理) as needed.

### Scan Depth: Quick vs. Deep

The dropdown next to `Full-Disk Scan This Drive` selects the scan depth:

| Level | Behavior | Best for |
|---|---|---|
| Quick (default, 1 level) | Only expands 1 level (the size of each first-level directory), so you see the big consumers first; **game installs always appear in the ranking** (not limited by depth); deeper directories remain clickable in the results for drill-down (scanned on the spot, slightly slower) | Almost all scenarios: locate the big consumers first, then drill down as needed — much faster than a deep scan from the start |
| Deep | Expands level by level along the thickest branches by size (up to 5 levels deep), digging straight to deep large items | When you want to see deep usage in one go; cheap if WizTree is installed |

The choice is remembered (survives a refresh); the default is **Quick**. Switching the level mid-scan invalidates the current scan and
restarts with the new level; results from Quick mode are labeled "Quick (1 level)" on the panel.
Note: Quick mode still needs to measure the size of each first-level directory (that's the main cost of a scan);
without WizTree, what it saves compared to Deep is the "deep exploration" part — it is not an instant overview.

### Duplicates and Selection Dedup: Whole Items Are Deleted, Never Split

The usage ranking may contain duplicate rows where "a large item and its children contain the same content with near-identical sizes". The handling rules:

- **Game installs (Steam / Epic / GOG) are one project — deleted whole, never split**: when the install row appears,
  its internal children no longer appear separately; if you check both the install and some item inside it, only the whole directory of the install is deleted.
- **Splittable directories such as archives and downloads**: children are shown as usual and can be checked individually; if both the outer and
  inner items are checked, only the smallest item is cleaned — no double deletion, and the estimated size won't double.
- Skipped duplicates are honestly reported in the cleanup results ("already inside the same game install, deleted whole without splitting"
  / "already covered by a smaller checked item, only the smallest is cleaned").

### Starting Drive and Scan Speedup

- **The starting drive defaults to a data drive**: when the interface opens, the first non-system drive is selected automatically (last-used drive takes priority).
  System drives have huge numbers of small files and are slowest to scan, and the main battlefield of cleanup is usually on data drives; with only one drive, C: is kept.
- **Instant drive switching**: full-disk scan results per drive are cached for 10 minutes, so switching back and forth shows results directly
  (the panel notes "scan results from N minutes ago"); clicking "Full-Disk Scan This Drive" forces a rescan, and the cache is automatically invalidated after cleanup.
- **To scan the system drive faster**: install WizTree (the interface offers a one-click download of the portable version) to read via the MFT,
  or keep the level at "Quick".

### Breakdown View: Which Part of a Directory Can Actually Be Deleted

In the "Full-Disk Scan", open a directory's details; at the bottom there is `Breakdown: What Can Be Deleted`. It splits a directory into two parts:

| | Content | Examples |
|---|---|---|
| ① Cleanable cache subdirectories | Derived data that rebuilds itself after deletion | `Cache`, `Code Cache`, `GPUCache`, `Service Worker` |
| ② Do not delete | User data and program bodies | Bookmarks, passwords, cookies, `History`, `Extensions`, game resource packs |

The reason this exists: a directory named like a browser can occupy hundreds of GB, and labeling it "cleanable" as a whole would delete bookmarks and passwords along with it,
while a single "keep this" note cannot explain which parts of those hundreds of GB can be touched. So it lists each subdirectory with its size and the reason.

It first determines the directory's actual identity, then decides what verdict to give:

- Browser profile directory: notes that it contains bookmarks and passwords, with the cache portion listed separately
- Game install directory: notes it is a game install, with cleanable bytes fixed at 0; use Steam uninstall to free the space
- Identity undeterminable: explicitly states "the tool does not judge", listing only the size distribution and giving no verdict

The judgment does not rely on the directory name. Same-named directories may hold theses, ComfyUI archives and other user content;
judging by name alone would falsely report "your bookmarks and passwords are here". So the structure `Cache` / `User Data`
must actually be observed inside the directory before it is judged a browser profile directory.

### Confirmation and Execution

Click the red "Clean Selected Items" (清理选中项) at the bottom right → a confirmation table pops up, listing each item's **exact path, size, and file count**;
medium/high-risk items and irreversible operations get red-frame warnings, and items lacking permissions are called out by name →
cleanup only starts when you click "Confirm Delete" (确认删除). When finished it reports the **actually freed amount** (not the estimate) and the skip reasons.

Files locked by browsers or video editors are **skipped rather than errored**, with a hint to "close the related programs and retry".

### What to Do When Files Are Locked

As long as the cleanup results contain "skipped" items — or an item was **aborted as a whole** because of locks (nothing deleted) — an extra **"Find Locking Processes" (一键查找占用进程)** button appears in the dialog. One click does two things:

1. **Confirms one by one** whether these files are actually locked right now (this step is a reliable determination);
2. Lists **the programs that might be holding them** — process name, PID, and "why it's considered suspicious".

**It only lists candidates; it will never close any process for you.** The reason is simple: Windows has no public API to map a "file handle" back to a "process", so step 2 is a **heuristic guess** that may miss or misreport; and closing the wrong process can lose someone's unsaved documents. The judgment and the action are both left to you.

Once you've spotted a suspicious program: **save your work and exit that program first** (most programs release the lock as soon as they exit), then come back to this page and clean again.

Getting zero candidates is also normal; in that case use the system's built-in troubleshooting tool:
**Task Manager → "Performance" → "Open Resource Monitor" at the bottom left → "CPU" → expand "Associated Handles" → type the file name into the search box**, and you'll see the process that truly holds it.

> Do not kill system processes like `System` or `explorer.exe`.

**Shortcuts for known holders**: some files can never be found in the process list — for example `content.bin` in the Windows Delivery Optimization cache is held by the Delivery Optimization service (DoSvc) living inside `svchost.exe`; its command line has no path and the binary is not in the cache folder. In such cases the dialog shows a hint directly: run `Stop-Service DoSvc` in an admin PowerShell, come back and clean again, then `Start-Service DoSvc` to restore (a reboot restores it too).

### Sidebar Panels: Collapsed Stacking with Independent Scrolling

The panels in the right sidebar are collapsed to a single title line by default; click the title to expand or collapse, and the expanded state is stored in the browser's local storage (key `oc.acc.v1`) and survives a refresh.

The left and right columns scroll independently: scrolling the list on the left does not move the panels on the right, and vice versa. When the window is narrower than 1180px it switches to whole-page scrolling so the two columns don't get squeezed too narrow.

For panels with buttons in their title (e.g. the download button in "Environment Self-Check" (运行环境自检)), clicking the button does not trigger expand/collapse.

### The Esc Key: Go Back One Level

- With a dialog open: closes the dialog
- In the usage tree: goes back to the parent directory; at the top level it says "already at the top-level directory"
- On the "Steam Game Library" page: returns to the scan overview
- Pressing `Esc` during a scan does not interrupt it; it only tells you to wait for the scan to finish

### Resource Packs: Supplementing and Overriding Item Explanations

The "Resource Packs" (资源包) panel in the sidebar lists currently loaded resource packs; each pack shows its entry count, type, and source (built-in / user), and can be enabled or disabled individually.

| Operation | Explanation |
|---|---|
| Import a resource pack | Choose a local `.json` file; after validation it is written to `config/packs-user/` |
| Check updates from GitHub | Downloads and imports each pack listed in the remote `packs/index.json`; when the id matches a built-in pack, it overrides the built-in version. The default remote is the public [O-Cleaner-packs](https://github.com/Oerian/O-Cleaner-packs) repository, and you can change it to your own address in the interface |
| Enable / disable | A disabled pack stops participating in identification, and the built-in list takes effect immediately (no restart needed) |

Matching and overriding rules:

- Pack entries match directories or files by "path fragments"; you can require all fragments to match (`all`) or any one (`any`)
- When multiple entries match simultaneously, **the one whose match sits closer to the end of the path wins**, then the longer match string. This rule exists so leaf directories (e.g. `steamapps\shadercache`) are not overridden by container directories (e.g. `\steamlibrary\steamapps`)
- For items **already recognized** by the built-in knowledge base: a pack can only rewrite the description texts (what it is / can it be rebuilt / deletion impact / why keep it) and the display name; `cleanable` and `risk` still follow the built-in rules
- For items **not recognized** by the built-in knowledge base: a pack may give a complete verdict, including whether it's cleanable and the risk level
- Changes and imports take effect immediately; refresh the page to see them

For the pack format and authoring rules, see [packs/README.md](../packs/README.md) in the repository.

### Connecting Your Own AI (Optional)

The "AI Assistant" panel in the sidebar is for connecting a large-model API you applied for yourself. **If left empty it does nothing at all**, and the program will not make any network requests because of it.

| Setting | Explanation |
|---|---|
| API endpoint | Any OpenAI-compatible `chat/completions` endpoint; filling up to the `/v1` level is enough. **DeepSeek / Ollama (local)** preset chips fill it in one click |
| Model name | Type it manually, or click **"Fetch list" (获取列表)** to auto-detect the models the endpoint offers and pick one from the suggestions |
| API Key | Stored in `config/ai.json` (already in `.gitignore`, never enters version control) |
| Temperature / timeout | Adjustable; defaults 0.2 / 60 seconds |

**Quick setup (DeepSeek as an example)**: register at platform.deepseek.com and create an API key → click the "DeepSeek" preset chip → paste the key → click "Fetch list" → pick a model (`deepseek-chat` for everyday use, `deepseek-reasoner` for reasoning) → check "Enabled" → "Save" → "Test connection". The five-step guide stays in the panel; local Ollama needs no key — just click its preset.

Two uses:

1. **Explain a single item**: click "Let AI Explain" (让 AI 解释) in the details dialog, and the AI gives a plain-language explanation combining the item's real path, size, and built-in description
2. **Generate a cleanup plan**: state your request in natural language (e.g. "only caches that rebuild without reinstalling, don't touch games"), and the AI returns a candidate list

Safety boundary: the candidate list only contains items **already judged cleanable by the built-in rules**; unknown ids returned by the AI are dropped and listed in the results; the AI cannot execute deletions — the plan can only check the checkboxes for you, and you still confirm and go through the same deletion flow behind the bottom buttons.

**Turn an AI plan into a rule**: after the AI checks the boxes for you, if you expect to reuse this selection, click **"Save Current Selection as Rule" (保存当前勾选为规则)** in the Custom Rules panel and give it a name — then one click applies it later, no need to ask the AI again. The whole chain is: AI proposes → checks boxes for you → you save it as a rule if satisfied → one-click run next time; every step has human confirmation, and the AI never touches the delete button.

The interface only echoes the last 4 characters of the Key; the full Key is never returned through any API.

---

## 3. Steam Game Library (Reads Manifests, Second-Level Speed)

The "Steam Game Library" panel in the right sidebar **does not scan the disk**; it directly reads Steam's own manifest files:

- `steamapps\libraryfolders.vdf` — lists all Steam libraries (possibly across multiple drives)
- `steamapps\appmanifest_*.acf` — each game's name, install directory, size, status, last-played time

Reading these manifests requires no disk traversal, so the time taken depends only on the number of games in the libraries, not the disk size.

The panel shows: game count, the total size recorded by Steam, and each library's location and usage; the game list can be sorted by
**"longest unplayed" or "by size"**, and directly shows information like `SomeGame 13.9 GB · never played` —
this is the most direct answer to "which game should I uninstall". Every entry has an "Open Folder" (打开目录) button.

Two things only reading manifests can do:

**① Cross-check against scan results.** The size recorded by Steam (`SizeOnDisk`) is its own calculation,
which can be compared against actual disk usage; the panel also lists **leftover directories that exist on disk but have no Steam record** —
such "incomplete uninstall" leftovers are usually the space most worth reclaiming.

**② Status verification.** It reads the `StateFlags` bits to determine whether a game is actually fully installed
(1=not installed, 4=installed, 2=updating, etc.), so a "half-updated" game is never treated as a complete install.

> Two ways to free a game's space: **uninstall in the Steam client** (right-click the game → Manage → Uninstall;
> it also cleans up the library record, leaving no leftovers), or in this tool **open the game → "Delete This Game" (删除这个游戏)**
> to delete the whole install directory directly (see below). Both reclaim space; the former is cleaner.

### Games Are Cleanable Items: Delete the Whole Game Directory Directly

Game installs (Steam / Epic / GOG) are marked **"cleanable"** in the interface — big titles you're not playing are
the space most worth reclaiming on a machine, often tens of GB at a time. How to operate:

1. On the **Steam Game Library** page (or in the usage tree of the full-disk scan), open a game;
2. The details dialog shows the **Chinese name**, size on disk, full path, and a save-data risk note;
3. Click the red **"Delete This Game" (删除这个游戏)**; the confirmation dialog lists the consequences again before it executes.

The deletion is **permanent** (it does not go to the Recycle Bin), so the confirmation dialog explicitly states:

- What is deleted is the **entire game directory**, including saves, configurations, and mods placed inside it;
- Most games actually keep their saves in `Documents\My Games`, `AppData`, or platform cloud saves, which are **unaffected**;
- The safer route is still uninstalling via the client; after deleting the directory directly, the client shows "not installed",
  and re-downloading is all it takes to play again.

**Safety boundary**: the deletion goes through the dedicated endpoint `POST /__oc__/clean-game`, which is **not** a generic deletion
endpoint — the path must be recognized **by the backend itself** as a game install directory (a direct
subdirectory of `steamapps\common`, or the exact install path in an Epic / GOG manifest), and then passes another round of system-directory protection
(System32, drive roots, relative paths, empty paths, etc. are all rejected). No matter what the frontend sends, it cannot bypass this check.

### Steam Games Are Also Integrated into the "Full-Disk Scan" (With Chinese Name Display)

No need to cross-reference between "disk space" and the "Steam panel": during scanning, the Steam manifests and the `config/game-names.json` mapping are read once,
and Chinese game names are shown directly in the usage tree and on the Steam page:

- `荒野大镖客：救赎 2（Red Dead Redemption 2）`
- `天国：拯救 2（Kingdom Come: Deliverance II）`
- `巫师 3：狂猎（The Witcher 3: Wild Hunt - Complete Edition）`
- `底特律：化身为人（Detroit: Become Human）`
- `文明 6（Sid Meier's Civilization VI）`

Games are listed directly in "Largest Items on the Disk" and the current-level list; the stiff separate "games and other large items" category is gone,
as are irrelevant internal depth labels like "level N", making the interface cleaner and more intuitive.
Game installs are marked **cleanable** (`cleanable=true`, risk level "high") — because games you're not playing
are exactly the space you should reclaim; the details dialog explains what deletion means, and whether to delete is your call.
To add or modify Chinese game names: edit `config/game-names.json` directly; it is purely for display, with zero side effects.

---

## 4. Scan Speed: Optional WizTree Acceleration

The full-disk scan has two paths; the fast one is used when WizTree is detected:

| | Method | Characteristics |
|---|---|---|
| Default | PowerShell recursive traversal | No extra components needed; can scan any directory |
| Accelerated | Calls WizTree to read the NTFS MFT | NTFS volumes only; time depends on disk type and file count |

Actual time depends on disk type (HDD / SATA SSD / NVMe), file count, and directory depth, so no fixed numbers are given.

### How the Progress Bar Is Calculated

Scan progress is reported by **real phases**, not estimated by elapsed time. The percentages in the table below are **overall progress**, not within-phase progress:

| Phase | Progress |
|---|---|
| Reading the disk MFT details with WizTree | Indeterminate (flowing stripes) |
| Parsing the exported details | By bytes read: 14% → 70% |
| Expanding the directory tree by size | Fixed 72% (single step, no finer breakdown) |
| Recursively summing directory sizes (fallback without WizTree) | By node budget: 10% → 72% |
| Reading Steam library manifests | Indeterminate (flowing stripes) |
| Reading Epic / GOG game manifests | Indeterminate (flowing stripes), fixed 78% |
| Identifying and annotating items one by one | By nodes processed: 84% → 94% |
| Organizing drill-down info | Fixed 96% |
| Done | 100% |

"Indeterminate" is used for steps whose total workload cannot be known in advance (reading the MFT, reading game manifests);
it shows flowing stripes with the note below: "the total workload of this step cannot be known in advance". The reason: earlier versions estimated progress with a time-decay formula like `1 - e^(-t/90)`; the faster the disk, the slower the progress climbed, which didn't match reality;
and when a scan got stuck, the percentage kept climbing anyway, masking the problem.

So "percentage frozen, stripes moving" is normal — it means the current step has no denominator.
Additionally, the server sets a timeout on PowerShell calls; on timeout it necessarily returns an error and continues with later phases,
never stalling on the same phase indefinitely.

The principle of the accelerated path is not parsing the MFT itself — that would require administrator rights plus handling USN journals and fragmentation,
and errors would yield wrong sizes — but calling `WizTree64.exe`'s export parameter and letting it do the work:

```
WizTree64.exe D:\ /export="%TEMP%\oc-wiztree-D-1234.csv"
```

### New-User Onboarding and Cross-Device Portability

At the top of the right sidebar on the home page is the "Environment Self-Check" (运行环境自检) panel, where new users — or anyone who copied the folder to a new computer — can confirm:

1. Whether the Python runtime is ready
2. Whether administrator rights are available (if not elevated, the scope of impact is explained)
3. The status of the WizTree acceleration component:
   - If installed on the system: called directly;
   - If not installed: the panel offers a "One-Click Download WizTree Portable (about 7.5 MB)" button,
     which downloads the portable version from the official address and extracts it to `portable\WizTree\` in this directory.

The extracted `portable\WizTree\` moves with the whole `O-Cleaner` folder;
when copied to a USB drive or handed to another device, no re-download is needed, no administrator rights are required, and nothing is written to the registry.

### Third-Party License and Notices, Stated Clearly

- Copyright and license: WizTree is copyrighted by Antibody Software Limited, free for personal use (PERSONAL USE ONLY).
- No-bundling principle: this project's distribution package does not embed the WizTree binary; the download is initiated by the user on their own machine.
- Commercial and distribution compliance: bundling WizTree with a commercial product requires purchasing the official
  [WizTree Distribution License](https://diskanalyzer.com/distribution-license).
- Export files are deleted after use: the exported CSV can reach hundreds of MB (entry count depends on disk contents);
  after parsing into memory it is deleted via `finally`; if the process is killed, leftover files are cleared on next startup.

### A Bonus: "Split Second-Level Directories" at Any Depth

WizTree's exported CSV contains the full path of every file, and directory rows' sizes already include all descendants.
So after parsing you get an in-memory "directory → size" table, and splitting any directory's children is a table lookup:

```
D:\some-cache-dir              → 17.9 GB
  └ webui                    → 14.9 GB   ▸ expandable
      └ sd-webui-aki-v4.11…  → 14.4 GB   ▸ expandable
          └ python           →  8.4 GB   ▸ expandable
```

Each level returned is one table lookup, not constrained by the scan tree's depth limit,
so the situation "the tree only expands to level 5 and deeper large items can't be opened" never happens.

When a directory has many children (e.g. thousands of files laid flat), expanding does not dump everything onto the page at once:
it first shows the top 50, and "Show More" (显示更多) appends 50 at a time;
if that level's total children exceed the per-response limit, it honestly notes "this level has N children in total; only the top M are listed by size".

---

## 5. Cumulative Cleanup and Conversion

**These two blocks are merged into one panel** ("Cumulative Cleanup and Conversion" (累积清理与换算)):

The top shows the cumulative freed total and cleanup count; right below is a card that converts the total into everyday units
(phone photos / 1080p movies / 4K movies / AAA games…), giving an instant sense of "what can the freed space hold".

When you want to dig deeper, expand "View Detailed Charts" (查看明细图表), which contains:

- **Per-drive breakdown** how much each drive freed
- **Trend chart** a bar chart of each cleanup's freed amount (older on the left, newer on the right; hover for details)
- **Cumulative by category** package managers / browsers / temporary files / GPU caches… and their shares
- **Top contributors** which cleanup items contributed most

Data is stored in `data\history.json` (up to 200 cleanups kept). The "Recent Cleanup Records" panel offers one-click clearing;
clearing also resets the cumulative statistics to zero.

---

## 6. Conversion Units (Customizable)

| Unit | Conversion basis |
|---|---|
| Phone photo | 3.5 MB |
| MP3 song | 8 MB |
| Office document | 256 KB |
| 1080p movie | 2 GB |
| 4K movie | 20 GB |
| AAA game | 48 GB |
| Hour of 1080p video | 3 GB |
| Installer package | 512 MB |
| Hour of 4K streaming | = 1/3 of a 4K movie (expression example) |
| GB | Conversion basis; not shown separately |

The capsule buttons at the top let you view just one unit, or all of them.

### How to Add Your Own Units

Add directly in the interface: **name** + **size** (a number + MB/GB/TB) → click "Add Unit" (添加单位).
If you don't want to type it, click "**Fill in Example**" (填入范例), which auto-fills "concert recording = 8 GB".

You can also edit `config/convert.json` directly. The code comments explain every field;
the key one is `bytes_per_unit` — it accepts a number, **and also an expression referencing other units**:

```json
{
  "id": "hour_4k_netflix",
  "label": "小时 4K 流媒体",
  "plural": "小时 4K 流媒体",
  "bytes_per_unit": "1/3 * movie4k",
  "note": "4K 流媒体约 7 GB/小时，这里用『4K 电影的 1/3』演示表达式换算",
  "category": "影音"
}
```

The entry above references `movie4k` (20 GB) and automatically computes ≈6.7 GB, no manual math needed.
Units with `unlimited: true` (like `gb`) don't participate in the "how many fit" display; they only serve as the conversion basis.

After editing `config/convert.json`, **just refresh the page** — no service restart needed.

---

## 7. Custom Rules (Save Fixed Selections, Apply in One Click)

A rule is not "typing in paths" — it **saves the group of cleanup items you checked**, for one-click reuse later.

Usage:

1. In the usage ranking, check the cleanup items you want (rows corresponding to cleanup targets; one-click presets work too), in any combination
2. In the "Custom Rules" (自定义规则) panel on the right, give the rule a name, e.g. "weekly cache cleanup"
3. Click "Save Current Selection as Rule" (保存当前勾选为规则)

After saving, the rule card shows: how many cleanup items it contains, **how much space it would free on the current drive**, and each item's name label.
Four actions:

| Button | Effect |
|---|---|
| Apply to Selection | Re-checks the items in this rule (without executing deletion); you can review before clicking the bottom cleanup button |
| Clean Now | Directly cleans this batch per the rule; a confirmation dialog shows the estimated freed amount before executing |
| Rename | Rename the rule |
| Delete | Delete the rule (only the rule; no files are affected) |

**Clean immediately after saving**: after checking this option in the panel, clicking save first asks you a question
(showing how many items will be cleaned and the estimated freed amount), and confirmed, cleanup executes immediately — done in one step.

A few design details:

- A rule **records the drive it was created on**. A rule created on drive D, after switching to drive C, has "Apply/Clean Now" greyed out,
  with the note "N items don't apply to the current drive", preventing deletion with the wrong drive's paths.
- Saving with the same name **overwrites** the original rule; no duplicate pile-up.
- A rule is a "selection set", not a cleanup target — so it doesn't appear in the main list, only in the sidebar.
- Rules are stored in `config/custom-rules.json`.

### Custom Data Pack: Export / Import All Three Kinds of Custom Data at Once

"Import/Export Custom Data" (导入 / 导出自定义数据) at the bottom of the panel packs three things into one JSON file:
**conversion units + explanation/verdict overrides + fixed-item rules**. Uses:

- **Machine migration**: export and take it with you; import on the new machine to restore (recommended together with copying the whole directory);
- **Have an AI generate it**: send the format spec from [docs/CUSTOMIZE.md](CUSTOMIZE.md) (or your exported file)
  to any AI assistant and describe your needs; import the JSON you get back. Import validates item by item;
  entries with wrong fields or referencing nonexistent cleanup items are skipped with the reason stated — send the reasons back to the AI for correction and re-export;
- **Backup**: export a copy before changing custom items.

An import never leaves half-finished results: the whole pack is format-validated first, the three subsections are accepted item by item independently, and the result is clearly reported.

---

## 8. Scheduled Automatic Cleanup

The "Scheduled Automatic Cleanup" (定时自动清理) panel: check to enable → set the interval in hours (0.25 = 15 minutes supported) →
choose which preset and which drive → save. Below the panel, the next and last execution times are shown.

**Note**: it depends on the service window running; if the window is closed, it won't execute.
If the scheduled time passed while it was closed, a catch-up run happens immediately after restart.

---

## 9. Directory Structure

```
O-Cleaner\
├─ 启动 O-Cleaner.bat      double-click to start
├─ app.py                  main program (scan / full-disk scan / API / deletion / conversion / resource packs / AI calls)
├─ README.md               project introduction
├─ docs\
│   └─ USAGE.md            this file (full usage guide)
├─ config\
│   ├─ drives.json         drive letters and app info (port, etc.)
│   ├─ targets.json        cleanup target definitions + one-click presets
│   ├─ filetypes.json      auto-identification knowledge base (the full-disk scan uses it to explain every item)
│   ├─ game-names.json     Chinese game name mapping (display only, freely extendable)
│   ├─ convert.json        conversion unit table (includes an expression example)
│   ├─ custom-rules.json   your saved custom rules (generated after running)
│   ├─ ai.json             your own AI API configuration (generated after running; contains the API Key; gitignored)
│   └─ packs-user\         your imported resource packs (generated after running; gitignored)
├─ packs\
│   ├─ README.md           resource pack format, matching semantics, and safety boundaries
│   ├─ index.json          pack manifest ("Check updates from GitHub" downloads according to it)
│   └─ *.json              built-in packs: windows-system / games-platform / games-titles / apps-common
├─ web\
│   └─ index.html          the interface (HTML + CSS + JS all in this one file)
├─ tools\
│   ├─ selftest.py         functional self-tests (300+ assertions)
│   ├─ lint_rules.py       rules and resource pack linter (safety gate before release/contribution)
│   ├─ _lint_selftest.py   reverse defect-injection self-test for the linter itself (knowledge base 15 classes + resource packs 20 classes + script templates 5 classes)
│   ├─ cdp_check.js        real-browser rendering check (uses system Edge/Chrome, zero install)
│   ├─ doctor.py           environment self-check for new devices
│   ├─ fix-encoding.ps1    the single canonical tool for the encoding and line-ending contract
│   ├─ make_package.py     package into a copyable zip (in-memory packaging, no staging directory)
│   ├─ sync_packs.py       consistency check and sync between the main repo's packs/ and the public distribution repo
│   └─ local-cases.json.example   clean template for real local cases
├─ data\history.json            cleanup history and cumulative statistics (generated after running)
├─ schedule.json           schedule settings (generated after running)
└─ LICENSE / CONTRIBUTING.md / SECURITY.md / HANDOFF.md
```

`data\history.json`, `schedule.json`, and `config/custom-rules.json` are runtime data;
to reset to factory state, just delete them (the program is unaffected).

> The repository also contains `.github\workflows\ci.yml` (CI's three jobs) and `.gitattributes`
> (the declaration of the line-ending and encoding contract). The former only runs on GitHub; the latter decides which line endings files must use.
> Before changing how repository files handle line endings, read the notes at the top of `tools/fix-encoding.ps1`.

### Run the Linter Before Contributing Rules

`config/filetypes.json` is the identification knowledge base of the full-disk scan; its rules decide which directories are judged cleanable.
So before submitting rules you must pass the safety gate:

```
python -X utf8 tools/lint_rules.py            # local check
python -X utf8 tools/lint_rules.py --strict    # warnings also count as failure (CI uses this)
```

The linter performs eight checks: structure validation, resource pack structure, semantic self-contradiction detection, keyword conflicts across rules,
verifying every keyword's owning rule with the real identification engine, documentation–configuration consistency, bracket balancing of embedded script templates,
and listing all "cleanable" rules for human review. Of these, the semantic-conflict and keyword-conflict checks surface the most substantive problems —
for example, the same keyword being shared by two rules with opposite recommendations.

---

## 10. Supported Drives / Portability

**All drive letters are supported** (C: through Z:), and any custom directory can be scanned too.

All paths use variables like `%LOCALAPPDATA%`, `%USERPROFILE%`, and `%DRIVE%`;
**no hardcoded drive letters and no hardcoded usernames**, so:

- Copy the whole `O-Cleaner` folder to a USB drive → copy it to another Windows PC → double-click the bat and it works
- That machine's drive letters, username, and free space are all recognized automatically
- The only requirement: Python 3 installed (no pip packages needed)

System items that don't exist on a data drive (like `D:\Windows\Temp`) are automatically filtered out,
provided the program can determine whether the drive is the system drive (by checking whether `\Windows\System32` exists).

---

## 11. Security Design

The following deletion targets are hard-rejected, even if `config/targets.json` is modified:

- Drive letter roots themselves, and deleting files directly at the root
- System directories like `Windows`, `Program Files`, `ProgramData`, `Users`, `Recovery`
- `System32`, `SysWOW64`, `WinSxS`, `Installer`, `servicing`, `Boot` at any level
- The `$Recycle.Bin` and `System Volume Information` directories themselves (the Recycle Bin goes through a dedicated cleanup endpoint)
- The user home directory `%USERPROFILE%` and `%USERPROFILE%\AppData`
- Non-absolute paths (like `C:`, `..\Windows`, `/etc`) — such inputs would resolve to the current working directory,
  so they are rejected outright

Things never suggested for cleanup include: user documents, pictures, desktop files, installed program bodies, downloaded AI models
(like `.ollama\models`), and the `Windows\Installer` and `WinSxS` component stores.

Deletion is a direct delete, not through the Recycle Bin. A confirmation dialog lists the exact paths and sizes before execution.
For the full guardrail list and the corresponding regression assertions, see [SECURITY.md](../SECURITY.md).

### The Parsing Layer's Contract: Return As-Is, Don't Guess Shapes for Callers

`run_ps()` returns the parsed JSON as-is after executing PowerShell: if the script outputs an object it is a `dict`; if it outputs an array it is a `list`.

This contract corresponds to a subtle defect: an earlier implementation said "if the result is an array, take the first element",
and the PowerShell used for directory size measurement happened to return a `[{path,size},…]` array. The consequence: in the breakdown view,
every directory's size except the first displayed as 0 — it looked like nothing was scanned, but the parsing layer was truncating the array.

The takeaway: the parsing layer doesn't guess shapes for callers; normalization, when needed, is done explicitly by the caller.
`tools/selftest.py` phase six pins "2 paths → 2 results" with an assertion to prevent this defect from recurring.

---

## 12. FAQ

**Q: The page won't open after starting?**
Look at the "interface address" line printed in the black window; the port may not be 8770 (it automatically moves on if occupied).

**Q: An item says "administrator rights required"?**
That's normal, not an error. `Windows\Temp`, `Windows\Logs\CBS`, the update download cache, etc. genuinely require administrator rights. Re-run the bat as administrator. When permissions are insufficient,
**the other items in the same batch are still cleaned normally**; one failure doesn't abort the rest.

**Q: It says "N items are in use"?**
Browsers, video editors, or Python processes are using those files. Close the related programs and clean again.

**Q: Games became laggy after cleanup?**
If you cleaned the "NVIDIA shader cache", the game needs to recompile shaders on first launch, causing brief stutter,
then it recovers automatically. This is the only side effect; it does not damage the game.

**Q: Do config changes require restarting the service?**
No. All `config\*.json` changes take effect by refreshing the page.

**Q: How to fully reset?**
Delete `data\history.json`, `schedule.json`, and `config\custom-rules.json`,
then restore `config\convert.json` to its default contents — that's a brand-new state.
