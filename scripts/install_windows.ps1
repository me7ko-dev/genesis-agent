#Requires -Version 5.1
<#
    scripts/install_windows.ps1 - install Genesis Agent on Windows.

    Windows is the platform where the install fails for reasons that have
    nothing to do with the agent, so this script checks those reasons first
    and names them, instead of letting pip fail three screens later:

      * `python` on a fresh Windows is the Microsoft Store stub - it opens the
        Store and installs nothing. Every candidate here is probed by actually
        asking it for its version, never by its name being on PATH.
      * pipx lands the `genesis` shim in a directory the CURRENT shell does not
        have on PATH yet, so `genesis` right after the install says "not
        recognized" on a perfectly good install. We call the shim by full path
        to verify, and say plainly that a new terminal is needed.
      * Git Bash decides whether the agent can run shell commands well. Without
        it the sandbox falls back to cmd.exe (see genesis_agent/sandbox.py,
        _windows_shell_prefix). `bash.exe` from System32/WindowsApps is the WSL
        launcher - a DIFFERENT operating system - and is deliberately not
        accepted, exactly as the runtime refuses it.

    All output is plain ASCII English on purpose. PowerShell 5.1 reads a .ps1
    without a BOM in the system codepage, and the console it prints to is
    cp866/cp1251 by default - Cyrillic here would be mangled before the user
    sees the first line. The Bulgarian explanation lives in docs/WINDOWS.md,
    which is read in an editor, not in a console.

    Usage:
        powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1
        ... -Ref main                  install a different branch or tag
        ... -SelfTest                  check this script's own logic, install nothing
#>
[CmdletBinding()]
param(
    [string]$Repo = "https://github.com/me7ko-dev/genesis-agent",
    [string]$Ref  = "claude/token-upgrade-ipe4yg",
    [switch]$SelfTest
)

$MinPython = [version]"3.10"

# Same list the sandbox probes at runtime; keep them in step.
$GitBashCandidates = @(
    "C:\Program Files\Git\bin\bash.exe",
    "C:\Program Files\Git\usr\bin\bash.exe",
    "C:\Program Files (x86)\Git\bin\bash.exe"
)


function Test-PythonVersionOk {
    <#  Is this version string at least 3.10. Anything unparseable is a no:
        a stub that prints a banner instead of a version must not pass.  #>
    param([string]$Version)
    if ([string]::IsNullOrWhiteSpace($Version)) { return $false }
    $clean = $Version.Trim()
    try { $parsed = [version]$clean } catch { return $false }
    return $parsed -ge $MinPython
}


function Test-WslLauncher {
    <#  bash.exe from System32 or WindowsApps is the WSL launcher, not a shell
        on this machine. Mirrors sandbox._is_wsl_launcher.  #>
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
    $low = $Path.Replace("/", "\").ToLower()
    return ($low -like "*\windowsapps\*") -or ($low -like "*\system32\*")
}


function Get-InterpreterVersion {
    <#  Ask the interpreter itself. Returns the version string, or "" if the
        candidate does not run, is the Store stub, or prints something else.  #>
    param([string]$Exe, [string[]]$PreArgs = @())
    $code = "import sys; print('%d.%d.%d' % sys.version_info[:3])"
    try {
        $out = & $Exe @($PreArgs + @("-c", $code)) 2>$null
    } catch {
        return ""
    }
    if ($LASTEXITCODE -ne 0) { return "" }
    if ($null -eq $out) { return "" }
    return ([string]($out | Select-Object -First 1)).Trim()
}


function Find-Python {
    <#  The first interpreter that is really there and really new enough.  #>
    $candidates = @(
        @{ Exe = "py";      PreArgs = @("-3") },
        @{ Exe = "python3"; PreArgs = @() },
        @{ Exe = "python";  PreArgs = @() }
    )
    foreach ($c in $candidates) {
        if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
        $version = Get-InterpreterVersion -Exe $c.Exe -PreArgs $c.PreArgs
        if (Test-PythonVersionOk $version) {
            return @{ Exe = $c.Exe; PreArgs = $c.PreArgs; Version = $version }
        }
    }
    return $null
}


function Find-GitBash {
    foreach ($path in $GitBashCandidates) {
        if (Test-Path $path) { return $path }
    }
    $found = Get-Command bash -ErrorAction SilentlyContinue
    if ($found -and -not (Test-WslLauncher $found.Source)) { return $found.Source }
    return $null
}


function Get-ShimPath {
    <#  Where pipx puts the command on Windows. Checked by existence, because
        PATH in this shell is stale until the user opens a new terminal.  #>
    $candidates = @(
        (Join-Path $env:USERPROFILE ".local\bin\genesis.exe"),
        (Join-Path $env:LOCALAPPDATA "pipx\pipx\venvs\genesis-agent\Scripts\genesis.exe")
    )
    foreach ($path in $candidates) {
        if ($path -and (Test-Path $path)) { return $path }
    }
    $onPath = Get-Command genesis -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    return $null
}


function Invoke-SelfTest {
    <#  Only the decisions, not the install: this part runs anywhere, so it
        can be checked before the script is ever pointed at a machine.  #>
    $failures = @()
    function Assert($condition, $message) {
        if (-not $condition) { $script:failures += $message }
    }
    $script:failures = @()

    Assert (Test-PythonVersionOk "3.10.0")  "3.10.0 must pass"
    Assert (Test-PythonVersionOk "3.12.1")  "3.12.1 must pass"
    Assert (Test-PythonVersionOk " 3.13 ")  "surrounding whitespace must not matter"
    Assert (-not (Test-PythonVersionOk "3.9.18")) "3.9 is below the floor"
    Assert (-not (Test-PythonVersionOk ""))        "empty version must fail"
    Assert (-not (Test-PythonVersionOk "Python was not found; run without arguments to install")) `
        "the Microsoft Store stub message must not read as a version"

    Assert (Test-WslLauncher "C:\Users\me\AppData\Local\Microsoft\WindowsApps\bash.EXE") `
        "the WindowsApps bash is the WSL launcher"
    Assert (Test-WslLauncher "C:/Windows/System32/bash.exe") `
        "forward slashes must not hide System32"
    Assert (-not (Test-WslLauncher "C:\Program Files\Git\bin\bash.exe")) `
        "Git Bash is a real shell"
    Assert (-not (Test-WslLauncher "")) "an empty path is not a launcher"

    Assert ((Get-InterpreterVersion -Exe "definitely-not-a-real-exe-42") -eq "") `
        "a missing interpreter yields no version, not an exception"

    if ($script:failures.Count -gt 0) {
        foreach ($f in $script:failures) { Write-Host "FAIL: $f" }
        return 1
    }
    Write-Host "OK"
    return 0
}


if ($SelfTest) { exit (Invoke-SelfTest) }


Write-Host ""
Write-Host "Genesis Agent - Windows install"
Write-Host "==============================="
Write-Host ""

$python = Find-Python
if ($null -eq $python) {
    Write-Host "No Python 3.10+ found."
    Write-Host ""
    Write-Host "Install it from https://www.python.org/downloads/windows/ and tick"
    Write-Host "'Add python.exe to PATH' in the installer. If typing 'python' opens"
    Write-Host "the Microsoft Store, that is the stub, not Python."
    exit 1
}
Write-Host ("Python:    {0} {1}" -f $python.Exe, $python.Version)

$bash = Find-GitBash
if ($bash) {
    Write-Host ("Git Bash:  {0}" -f $bash)
} else {
    Write-Host "Git Bash:  not found - shell commands will fall back to cmd.exe."
    Write-Host "           Recommended: install Git for Windows (https://git-scm.com/download/win)."
}
Write-Host ""

$spec = "git+{0}@{1}" -f $Repo, $Ref
Write-Host ("Installing {0}" -f $spec)
Write-Host "(this pulls the package and its dependencies; it takes a minute)"
Write-Host ""

& $python.Exe @($python.PreArgs + @("-m", "pip", "install", "--quiet", "--user", "--upgrade", "pipx"))
if ($LASTEXITCODE -ne 0) {
    Write-Host "Could not install pipx. The error from pip is above."
    exit 1
}

& $python.Exe @($python.PreArgs + @("-m", "pipx", "ensurepath"))

& $python.Exe @($python.PreArgs + @("-m", "pipx", "install", "--force", $spec))
if ($LASTEXITCODE -ne 0) {
    Write-Host "The install failed. The error from pipx is above."
    exit 1
}

$shim = Get-ShimPath
if ($null -eq $shim) {
    Write-Host ""
    Write-Host "Installed, but the 'genesis' command was not found where pipx usually"
    Write-Host "puts it. Open a NEW terminal and try 'genesis --version'."
    exit 1
}

Write-Host ""
$reported = & $shim --version 2>&1
Write-Host ("Installed: {0}" -f (([string]($reported | Select-Object -First 1)).Trim()))
Write-Host ("Command:   {0}" -f $shim)
Write-Host ""
Write-Host "Next:"
Write-Host "  1. Open a NEW terminal - this one does not have the new PATH yet."
Write-Host "  2. genesis setup     - asks for API keys and tests each one live."
Write-Host "  3. genesis           - start working."
Write-Host ""
Write-Host "Tip: use Windows Terminal, or run 'chcp 65001' first. The agent writes"
Write-Host "     Bulgarian, and the default console codepage cannot show it."
Write-Host ""
