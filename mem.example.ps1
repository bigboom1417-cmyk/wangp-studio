# Frees RAM + GPU before a studio job and puts everything back after.
#   mem.ps1 free     -> remembers what is running from $apps, then stops it
#   mem.ps1 restore  -> starts again what "free" stopped
# Blender, Excel and the browsers are left alone: they can hold unsaved work.
param([Parameter(Mandatory)][ValidateSet('free', 'restore')]$do)
$state = "$PSScriptRoot\mem-stopped.json"
# match = regex on the command line; start = how to bring it back (hidden)
$apps = @(
  @{ name = 'Wallpaper Engine';          match = 'wallpaper_engine\\wallpaper(32|64)\.exe'; start = '"C:\Program Files (x86)\Steam\steamapps\common\wallpaper_engine\wallpaper64.exe"' }
  @{ name = 'Steam';                     match = '\\Steam\\steam\.exe';         start = '"C:\Program Files (x86)\Steam\steam.exe" -silent' }
  @{ name = 'Copilot';                   match = 'Copilot\\Application\\copilotapp\.exe'; start = '' }
  # the overlay leaks (3.2 GB seen) and its container respawns it: container first (the NVIDIA service starts a fresh one without the overlay), then the overlay
  @{ name = 'NVIDIA Overlay container';  match = 'NvContainer\\plugins\\SPUser'; start = '' }
  @{ name = 'NVIDIA Overlay';            match = 'CEF\\NVIDIA Overlay\.exe';    start = '"C:\Program Files\NVIDIA Corporation\NVIDIA App\CEF\NVIDIA Overlay.exe"' }
)
function Read-State { $l = Get-Content $state -Raw | ConvertFrom-Json; if ($l.PSObject.Properties['value']) { $l.value } else { $l } }   # not "if ($l.value)": on a list of 2+ that is a list of nulls and restore started nothing   # PowerShell 5 sometimes wraps the list in {value, Count}
if ($do -eq 'free') {
  $procs = Get-CimInstance Win32_Process
  # freed before and something came back: stop it again, keep the first list
  $stopped = @(if (Test-Path $state) { Read-State })
  foreach ($a in $apps) {
    $hit = @($procs | Where-Object { "$($_.ExecutablePath) $($_.CommandLine)" -match $a.match })   # the voice server's command line is only "python_embedded\python.exe server.py"
    if (!$hit) { continue }
    $hit | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    if ($stopped.name -notcontains $a.name) { $stopped += @{ name = $a.name; start = $a.start } }
    "stopped $($a.name)"
  }
  ConvertTo-Json @($stopped) | Set-Content $state -Encoding utf8
} else {
  if (!(Test-Path $state)) { 'nothing to restore'; exit 0 }
  foreach ($s in @(Read-State)) {
    if ($s.start) { Start-Process cmd.exe -ArgumentList '/c', "start `"`" $($s.start)" -WindowStyle Hidden; "started $($s.name)" }
  }
  Remove-Item $state
}
$os = Get-CimInstance Win32_OperatingSystem
"free RAM {0:n1} GB, GPU used {1} MiB" -f ($os.FreePhysicalMemory / 1MB), (nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits)
