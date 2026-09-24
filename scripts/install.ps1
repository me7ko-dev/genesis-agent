<#
    scripts/install.ps1 - Genesis Agent as a native Windows app.

    One line in PowerShell, nothing to install first (no Python, no pipx):

        irm https://raw.githubusercontent.com/me7ko-dev/genesis-agent/main/scripts/install.ps1 | iex

    What it does:
      * downloads genesis-windows-x64.zip from the latest GitHub release
        (built from main by .github/workflows/native.yml) and checks it
        against the SHA256 published beside it;
      * unpacks it next to the install directory, starts the new genesis.exe
        once, and only then swaps it in - a broken download never replaces
        a working install;
      * puts the install directory on the user PATH, adds a Start menu entry
        (in Windows Terminal when there is one) and an "Apps" entry, so it
        uninstalls like any other app;
      * checks Git Bash (the agent's shell; offers winget when missing) and
        says whether a system Python is there for your projects' tests.

    The same script is shipped inside the app and is what /update runs: it
    waits for the running genesis.exe to exit (-WaitPid), then does the above
    without prompts and records the result for the next start (-StateFile).

    Plain ASCII English on purpose: PowerShell 5.1 reads a .ps1 without a BOM
    in the system codepage. Never `exit` outside -File mode: under `iex` that
    would close the user's PowerShell window.

    Usage:
        ... | iex                                   install or update
        powershell -File install.ps1 -Uninstall     remove the app (keeps ~/.genesis)
        powershell -File install.ps1 -Uninstall -Purge   ... and keys, memory, skills
        powershell -File install.ps1 -ZipPath x.zip install a local build
        powershell -File install.ps1 -SelfTest      check this script's logic
#>
param(
    [string]$Repo = "me7ko-dev/genesis-agent",
    [string]$Tag = "latest",
    [string]$InstallDir = "",
    [string]$ZipPath = "",
    [int]$WaitPid = 0,
    [int]$WaitTimeout = 120,
    [string]$StateFile = "",
    [switch]$NoShortcut,
    [switch]$NoPath,
    [switch]$NoPrompt,
    [switch]$Uninstall,
    [switch]$Purge,
    [switch]$SelfTest
)

$GenesisAsset = "genesis-windows-x64.zip"
$GenesisUninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\GenesisAgent"

# Same places genesis_agent/sandbox.py (_git_bash_candidates) looks.
function Get-GitBashCandidates {
    $list = @()
    if ($env:GENESIS_GIT_BASH) { $list += $env:GENESIS_GIT_BASH }
    $list += "C:\Program Files\Git\bin\bash.exe"
    $list += "C:\Program Files\Git\usr\bin\bash.exe"
    $list += "C:\Program Files (x86)\Git\bin\bash.exe"
    if ($env:LOCALAPPDATA) { $list += (Join-Path $env:LOCALAPPDATA "Programs\Git\bin\bash.exe") }
    return $list
}


function Write-Step { param([string]$Text) Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Text) Write-Host "    [ok] $Text" -ForegroundColor Green }
function Write-Warn { param([string]$Text) Write-Host "    [!]  $Text" -ForegroundColor Yellow }


function Get-DefaultInstallDir {
    return (Join-Path $env:LOCALAPPDATA "Programs\Genesis")
}


function Get-GenesisHome {
    if ($env:GENESIS_HOME) { return $env:GENESIS_HOME }
    return (Join-Path $HOME ".genesis")
}


function Test-WslLauncher {
    <#  bash.exe from System32 or WindowsApps is the WSL launcher, not a shell
        on this machine. Mirrors sandbox._is_wsl_launcher.  #>
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
    $low = $Path.Replace("/", "\").ToLower()
    return ($low -like "*\windowsapps\*") -or ($low -like "*\system32\*")
}


function Add-PathEntry {
    <#  $PathValue with $Dir appended, unless an equal entry is already there
        (case, trailing backslash and %VARS% ignored).  #>
    param([string]$PathValue, [string]$Dir)
    $want = [Environment]::ExpandEnvironmentVariables($Dir).TrimEnd("\").ToLower()
    $parts = @($PathValue -split ";" | Where-Object { $_ -ne "" })
    foreach ($p in $parts) {
        if ([Environment]::ExpandEnvironmentVariables($p).TrimEnd("\").ToLower() -eq $want) {
            return ($parts -join ";")
        }
    }
    return (($parts + $Dir) -join ";")
}


function Remove-PathEntry {
    param([string]$PathValue, [string]$Dir)
    $want = [Environment]::ExpandEnvironmentVariables($Dir).TrimEnd("\").ToLower()
    $parts = @($PathValue -split ";" | Where-Object {
        $_ -ne "" -and [Environment]::ExpandEnvironmentVariables($_).TrimEnd("\").ToLower() -ne $want
    })
    return ($parts -join ";")
}


function Get-ExpectedHash {
    <#  The hash from a `sha256sum`-style line ("<hex>  <name>"), or "".  #>
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return "" }
    $first = ($Text.Trim() -split "\s+")[0]
    if ($first -match "^[0-9a-fA-F]{64}$") { return $first.ToLower() }
    return ""
}


function ConvertTo-JsonString {
    <#  A JSON string literal. ConvertTo-Json on 5.1 escapes differently from
        7 and mangles non-ASCII in some locales; the state file is read by
        Python's json module, which only needs valid escapes.  #>
    param([string]$Text)
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    foreach ($ch in $Text.ToCharArray()) {
        $code = [int]$ch
        if ($ch -eq '"') { [void]$sb.Append('\"') }
        elseif ($ch -eq '\') { [void]$sb.Append('\\') }
        elseif ($code -lt 32 -or $code -gt 126) { [void]$sb.Append(('\u{0:x4}' -f $code)) }
        else { [void]$sb.Append($ch) }
    }
    [void]$sb.Append('"')
    return $sb.ToString()
}


function Write-State {
    <#  update_state.json for genesis_agent.self_update.report_pending.  #>
    param([string]$Path, [bool]$Ok, [string]$Message)
    if ([string]::IsNullOrWhiteSpace($Path)) { return }
    $ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $key = if ($Ok) { "spec" } else { "error" }
    $okText = if ($Ok) { "true" } else { "false" }
    $json = "{`"ts`": $ts, `"ok`": $okText, `"$key`": $(ConvertTo-JsonString $Message)}"
    try {
        $dir = Split-Path -Parent $Path
        if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        [IO.File]::WriteAllText($Path, $json, (New-Object System.Text.UTF8Encoding($false)))
    } catch {
        Write-Warn "could not write $Path : $($_.Exception.Message)"
    }
}


function Wait-ProcessExit {
    param([int]$ProcessId, [int]$TimeoutSeconds = 120)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) {
        if ((Get-Date) -gt $deadline) {
            throw "genesis (pid $ProcessId) did not exit within $TimeoutSeconds s"
        }
        Start-Sleep -Milliseconds 500
    }
}


function Get-RunningFromDir {
    <#  genesis.exe processes started from $Dir - they keep its files locked.  #>
    param([string]$Dir)
    $exe = (Join-Path $Dir "genesis.exe").ToLower()
    return @(Get-Process -Name genesis -ErrorAction SilentlyContinue | Where-Object {
        try { $_.Path -and $_.Path.ToLower() -eq $exe } catch { $false }
    })
}


function Invoke-Download {
    param([string]$Url, [string]$OutFile)
    for ($i = 1; $i -le 4; $i++) {
        try {
            Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing -Headers @{ "User-Agent" = "genesis-installer" }
            return
        } catch {
            if ($i -eq 4) { throw "download failed: $Url ($($_.Exception.Message))" }
            Start-Sleep -Seconds ([math]::Pow(2, $i))
        }
    }
}


function Get-ReleaseBase {
    <#  "latest" follows GitHub's latest release, which switches over only
        once all of a release's files are uploaded - never a new zip with an
        old checksum. Any other tag pins that release.  #>
    param([string]$RepoName, [string]$TagName)
    if ($TagName -eq "latest") { return "https://github.com/$RepoName/releases/latest/download" }
    return "https://github.com/$RepoName/releases/download/$TagName"
}


function Get-Package {
    <#  Path to a verified zip: -ZipPath as given, or downloaded from the release.  #>
    param([string]$Temp)
    if ($ZipPath) {
        if (-not (Test-Path $ZipPath)) { throw "no such file: $ZipPath" }
        $zip = (Resolve-Path $ZipPath).Path
        $sumFile = "$zip.sha256"
    } else {
        $base = Get-ReleaseBase -RepoName $Repo -TagName $Tag
        $zip = Join-Path $Temp $GenesisAsset
        $sumFile = "$zip.sha256"
        Write-Step "Downloading $GenesisAsset ($Tag)"
        Invoke-Download -Url "$base/$GenesisAsset" -OutFile $zip
        Invoke-Download -Url "$base/$GenesisAsset.sha256" -OutFile $sumFile
    }
    if (Test-Path $sumFile) {
        $expected = Get-ExpectedHash (Get-Content -Raw $sumFile)
        $actual = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLower()
        if (-not $expected) { throw "unreadable checksum file $sumFile" }
        if ($expected -ne $actual) { throw "checksum mismatch: expected $expected, got $actual" }
        Write-Ok "SHA256 verified"
    } elseif (-not $ZipPath) {
        throw "the release has no checksum for $GenesisAsset"
    }
    return $zip
}


function Install-Files {
    <#  Unpack beside $Dir, prove the new exe starts, then swap directories.
        Returns the new version line.  #>
    param([string]$Zip, [string]$Dir)
    $parent = Split-Path -Parent $Dir
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    $staging = "$Dir.new"
    $old = "$Dir.old"
    foreach ($d in @($staging, $old)) {
        if (Test-Path $d) { Remove-Item -Recurse -Force $d -ErrorAction SilentlyContinue }
    }
    Write-Step "Unpacking"
    Expand-Archive -Path $Zip -DestinationPath $staging -Force
    $newExe = Join-Path $staging "genesis.exe"
    if (-not (Test-Path $newExe)) { throw "genesis.exe is not in the archive" }

    # Antivirus quarantine and a missing VC runtime both show up here, before
    # anything that works is touched.
    # "Continue" while it runs: under "Stop", 5.1 turns any stderr line of a
    # native command into a terminating NativeCommandError.
    $eap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $version = (& $newExe --version 2>&1 | Out-String).Trim() }
    finally { $ErrorActionPreference = $eap }
    if ($LASTEXITCODE -ne 0 -or $version -notlike "genesis-agent*") {
        throw "the new genesis.exe does not start: $version"
    }
    Write-Ok $version

    if (Test-Path $Dir) {
        $running = Get-RunningFromDir $Dir
        if ($running.Count -gt 0) {
            throw "Genesis is still running (pid $(($running | ForEach-Object { $_.Id }) -join ', ')). Close every Genesis window and run the installer again."
        }
        $moved = $false
        for ($i = 0; $i -lt 10 -and -not $moved; $i++) {
            try { Move-Item -Path $Dir -Destination $old -ErrorAction Stop; $moved = $true }
            catch { Start-Sleep -Seconds 1 }
        }
        if (-not $moved) { throw "$Dir is in use (an open terminal inside it, or antivirus scanning it)" }
    }
    try {
        Move-Item -Path $staging -Destination $Dir -ErrorAction Stop
    } catch {
        if (Test-Path $old) { Move-Item -Path $old -Destination $Dir -ErrorAction SilentlyContinue }
        throw
    }
    if (Test-Path $old) { Remove-Item -Recurse -Force $old -ErrorAction SilentlyContinue }
    return $version
}


function Set-UserPath {
    param([string]$Dir, [switch]$Remove)
    $key = Get-Item -Path "HKCU:\Environment"
    # Read unexpanded: writing back the expanded value would freeze every
    # %VAR% entry in the user's PATH.
    $current = [string]$key.GetValue("Path", "", [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    $new = if ($Remove) { Remove-PathEntry $current $Dir } else { Add-PathEntry $current $Dir }
    if ($new -ne $current) {
        Set-ItemProperty -Path "HKCU:\Environment" -Name Path -Value $new -Type ExpandString
    }
    # Setting a variable through .NET broadcasts WM_SETTINGCHANGE, so new
    # terminals see the PATH change without logging off.
    $value = if ($Remove) { $null } else { $Dir }
    [Environment]::SetEnvironmentVariable("GENESIS_INSTALL_DIR", $value, "User")
    if ($Remove) {
        $env:Path = Remove-PathEntry $env:Path $Dir
    } else {
        $env:Path = Add-PathEntry $env:Path $Dir
    }
}


function Get-ShortcutPath {
    return (Join-Path ([Environment]::GetFolderPath("Programs")) "Genesis Agent.lnk")
}


function New-StartMenuShortcut {
    param([string]$Dir)
    $exe = Join-Path $Dir "genesis.exe"
    $work = Join-Path (Get-GenesisHome) "workspace"
    if (-not (Test-Path $work)) { New-Item -ItemType Directory -Path $work -Force | Out-Null }
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut((Get-ShortcutPath))
    $wt = Get-Command wt.exe -ErrorAction SilentlyContinue
    if ($wt) {
        $lnk.TargetPath = $wt.Source
        $lnk.Arguments = "--title Genesis -d `"$work`" `"$exe`""
    } else {
        $lnk.TargetPath = $exe
        $lnk.Arguments = ""
    }
    $lnk.WorkingDirectory = $work
    $lnk.IconLocation = "$exe,0"
    $lnk.Description = "Genesis Agent - autonomous coding agent"
    $lnk.Save()
}


function Register-App {
    param([string]$Dir, [string]$Version)
    $script = Join-Path $Dir "_internal\install.ps1"
    $sizeKb = [int]((Get-ChildItem -Recurse -File $Dir | Measure-Object -Sum Length).Sum / 1024)
    $ver = ($Version -replace "^genesis-agent\s*", "")
    if (-not (Test-Path $GenesisUninstallKey)) { New-Item -Path $GenesisUninstallKey -Force | Out-Null }
    $values = @{
        DisplayName     = "Genesis Agent"
        DisplayVersion  = $ver
        Publisher       = "Genesis Agent"
        InstallLocation = $Dir
        DisplayIcon     = (Join-Path $Dir "genesis.exe")
        URLInfoAbout    = "https://github.com/$Repo"
        UninstallString = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$script`" -Uninstall -InstallDir `"$Dir`""
    }
    foreach ($k in $values.Keys) {
        Set-ItemProperty -Path $GenesisUninstallKey -Name $k -Value $values[$k]
    }
    Set-ItemProperty -Path $GenesisUninstallKey -Name NoModify -Value 1 -Type DWord
    Set-ItemProperty -Path $GenesisUninstallKey -Name NoRepair -Value 1 -Type DWord
    Set-ItemProperty -Path $GenesisUninstallKey -Name EstimatedSize -Value $sizeKb -Type DWord
}


function Find-GitBash {
    foreach ($c in Get-GitBashCandidates) {
        if ($c -and (Test-Path $c)) { return $c }
    }
    $found = Get-Command bash.exe -ErrorAction SilentlyContinue
    if ($found -and -not (Test-WslLauncher $found.Source)) { return $found.Source }
    return ""
}


function Find-SystemPython {
    <#  The Python a user project's tests will run with (paths.project_python).
        WindowsApps is skipped: with no Python installed it is the Store stub.  #>
    foreach ($cand in @(@("py", "-3"), @("python"), @("python3"))) {
        $cmd = Get-Command $cand[0] -ErrorAction SilentlyContinue
        if (-not $cmd -or $cmd.Source.ToLower().Contains("\windowsapps\")) { continue }
        $pre = @()
        if ($cand.Count -gt 1) { $pre = $cand[1..($cand.Count - 1)] }
        $eap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $out = & $cmd.Source @($pre + @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")) 2>$null
            if ($LASTEXITCODE -eq 0 -and "$out" -match "^\d+\.\d+") { return "$($cand -join ' ') ($out)" }
        } catch { } finally { $ErrorActionPreference = $eap }
    }
    return ""
}


function Test-Prerequisites {
    param([bool]$Interactive, [string]$Dir)
    Write-Step "Checking what the agent uses on this machine"
    $bash = Find-GitBash
    if ($bash) {
        Write-Ok "Git Bash: $bash"
    } else {
        Write-Warn "Git for Windows is missing. The agent runs shell commands through Git Bash;"
        Write-Warn "without it they fall back to cmd.exe, where half of them behave differently."
        $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
        if ($winget -and $Interactive) {
            $answer = Read-Host "    Install Git for Windows now with winget? [Y/n]"
            if ($answer -eq "" -or $answer -match "^[yY]") {
                & $winget.Source install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements | Out-Host
                if (Find-GitBash) { Write-Ok "Git Bash installed" } else { Write-Warn "winget finished, but bash.exe is still not found" }
            }
        } else {
            Write-Warn "Install it from https://git-scm.com/download/win (or: winget install Git.Git)"
        }
    }
    $py = Find-SystemPython
    if ($py) {
        Write-Ok "Python for your projects: $py"
    } else {
        Write-Warn "No Python on this machine. Genesis itself does not need one (it carries its own),"
        Write-Warn "but testing YOUR Python projects does: winget install Python.Python.3.12"
    }
    # A pipx install earlier on PATH would keep answering to `genesis`.
    $resolved = Get-Command genesis -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $mine = (Join-Path $Dir "genesis.exe").ToLower()
    if ($resolved -and $resolved.Source.ToLower() -ne $mine) {
        Write-Warn "'genesis' still starts $($resolved.Source) (an older pip/pipx install)."
        Write-Warn "Remove it so the app answers: py -m pipx uninstall genesis-agent"
    }
    if (-not (Get-Command wt.exe -ErrorAction SilentlyContinue)) {
        Write-Warn "Windows Terminal is recommended (Cyrillic and colours): winget install Microsoft.WindowsTerminal"
    }
}


function Invoke-Install {
    $dir = if ($InstallDir) { $InstallDir } else { Get-DefaultInstallDir }
    $interactive = (-not $NoPrompt) -and ($WaitPid -eq 0) -and [Environment]::UserInteractive
    $temp = Join-Path ([IO.Path]::GetTempPath()) ("genesis-install-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
    New-Item -ItemType Directory -Path $temp -Force | Out-Null
    try {
        if ($WaitPid -gt 0) {
            Write-Step "Waiting for genesis (pid $WaitPid) to exit"
            Wait-ProcessExit -ProcessId $WaitPid -TimeoutSeconds $WaitTimeout
        }
        $zip = Get-Package -Temp $temp
        $version = Install-Files -Zip $zip -Dir $dir
        Write-Ok "Installed in $dir"
        if (-not $NoPath) { Set-UserPath -Dir $dir; Write-Ok "On the user PATH" }
        if (-not $NoShortcut) {
            try { New-StartMenuShortcut -Dir $dir; Write-Ok "Start menu: Genesis Agent" }
            catch { Write-Warn "no Start menu entry: $($_.Exception.Message)" }
        }
        try { Register-App -Dir $dir -Version $version } catch { Write-Warn "no Apps entry: $($_.Exception.Message)" }
        if ($WaitPid -eq 0) { Test-Prerequisites -Interactive $interactive -Dir $dir }
        Write-State -Path $StateFile -Ok $true -Message $version
        return $version
    } catch {
        Write-State -Path $StateFile -Ok $false -Message $_.Exception.Message
        throw
    } finally {
        Remove-Item -Recurse -Force $temp -ErrorAction SilentlyContinue
    }
}


function Invoke-Uninstall {
    $dir = if ($InstallDir) { $InstallDir } else { Get-DefaultInstallDir }
    $running = Get-RunningFromDir $dir
    if ($running.Count -gt 0) { throw "Genesis is running - close every Genesis window first." }
    Write-Step "Removing Genesis Agent from $dir"
    Set-UserPath -Dir $dir -Remove
    $lnk = Get-ShortcutPath
    if (Test-Path $lnk) { Remove-Item -Force $lnk }
    if (Test-Path $GenesisUninstallKey) { Remove-Item -Recurse -Force $GenesisUninstallKey }
    if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
    Write-Ok "Removed"
    $gh = Get-GenesisHome
    if ($Purge) {
        if (Test-Path $gh) { Remove-Item -Recurse -Force $gh; Write-Ok "Removed $gh (keys, memory, skills)" }
    } elseif (Test-Path $gh) {
        Write-Host "    Your keys, memory and skills stay in $gh (-Purge removes them)."
    }
}


function Invoke-SelfTest {
    $count = @{ failed = 0 }
    function Check([string]$Name, [bool]$Cond) {
        if ($Cond) { Write-Host "  ok   $Name" }
        else { Write-Host "  FAIL $Name" -ForegroundColor Red; $count.failed++ }
    }
    $p = "C:\a;%USERPROFILE%\bin;C:\b\"
    Check "adds a missing entry" ((Add-PathEntry $p "C:\genesis") -eq "$p;C:\genesis")
    Check "keeps an existing entry (case, trailing \)" ((Add-PathEntry $p "c:\B") -eq $p)
    Check "adds to an empty PATH" ((Add-PathEntry "" "C:\g") -eq "C:\g")
    Check "keeps %VAR% entries unexpanded" ((Add-PathEntry $p "C:\x").Contains("%USERPROFILE%\bin"))
    Check "removes an entry" ((Remove-PathEntry "C:\a;C:\genesis\;C:\b" "C:\Genesis") -eq "C:\a;C:\b")
    Check "remove leaves others" ((Remove-PathEntry "C:\a" "C:\g") -eq "C:\a")
    $h = "a" * 64
    Check "reads a sha256sum line" ((Get-ExpectedHash "$h  genesis-windows-x64.zip") -eq $h)
    Check "rejects a non-hash" ((Get-ExpectedHash "Not Found") -eq "")
    Check "WSL launcher refused" (Test-WslLauncher "C:\Users\x\AppData\Local\Microsoft\WindowsApps\bash.exe")
    Check "Git Bash accepted" (-not (Test-WslLauncher "C:\Program Files\Git\bin\bash.exe"))
    Check "JSON escaping" ((ConvertTo-JsonString "a`"b\c") -eq '"a\"b\\c"')
    $tmp = Join-Path ([IO.Path]::GetTempPath()) "genesis-selftest-state.json"
    Write-State -Path $tmp -Ok $false -Message "C:\x `"y`""
    $state = Get-Content -Raw $tmp | ConvertFrom-Json
    Check "state file is valid JSON" (($state.ok -eq $false) -and ($state.error -eq "C:\x `"y`""))
    Remove-Item -Force $tmp -ErrorAction SilentlyContinue
    Check "latest release URL" ((Get-ReleaseBase "o/r" "latest") -eq "https://github.com/o/r/releases/latest/download")
    Check "pinned release URL" ((Get-ReleaseBase "o/r" "native-build-7") -eq "https://github.com/o/r/releases/download/native-build-7")
    Check "default install dir under LOCALAPPDATA" ((Get-DefaultInstallDir).StartsWith($env:LOCALAPPDATA))
    if ($count.failed -gt 0) { Write-Host "$($count.failed) check(s) failed" -ForegroundColor Red; return 1 }
    Write-Host "OK"
    return 0
}


function Invoke-GenesisMain {
    $ErrorActionPreference = "Stop"
    $ProgressPreference = "SilentlyContinue"   # 5.1's progress bar makes downloads ~10x slower
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    if ($SelfTest) { return (Invoke-SelfTest) }
    if (-not [Environment]::Is64BitOperatingSystem) {
        Write-Host "Genesis Agent needs 64-bit Windows." -ForegroundColor Red
        return 1
    }
    try {
        if ($Uninstall) { Invoke-Uninstall; return 0 }
        $version = Invoke-Install
    } catch {
        Write-Host ""
        Write-Host "Genesis Agent was not installed: $($_.Exception.Message)" -ForegroundColor Red
        return 1
    }
    if ($WaitPid -eq 0) {
        Write-Host ""
        Write-Host "$version is installed." -ForegroundColor Green
        Write-Host "  genesis setup    API keys (once; one free key is enough)"
        Write-Host "  genesis          start, in the folder you want to work in"
        Write-Host "It already works in this window; other open terminals need a restart."
    }
    return 0
}


$GenesisExitCode = Invoke-GenesisMain
# -File: a real exit code for CI and for /update. Under `irm | iex` this is
# not an external script, and `exit` would close the user's window.
if ($MyInvocation.MyCommand.CommandType -eq "ExternalScript") { exit $GenesisExitCode }
