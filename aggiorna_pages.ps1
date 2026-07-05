#Requires -Version 5.1
<#
  aggiorna_pages.ps1 — Moto Monitor

  Esegue un giro del monitor (dal TUO PC, quindi con IP domestico: lo scraping
  funziona) e pubblica la pagina aggiornata su GitHub Pages con un commit+push.
  Sa anche registrarsi in Utilita' di pianificazione per partire a ogni accesso
  a Windows.

  USO
    Esecuzione singola (fa il giro e il push):
      powershell -NoProfile -ExecutionPolicy Bypass -File .\aggiorna_pages.ps1

    Installa l'avvio automatico al login:
      powershell -NoProfile -ExecutionPolicy Bypass -File .\aggiorna_pages.ps1 -Install

    Rimuovi l'avvio automatico:
      powershell -NoProfile -ExecutionPolicy Bypass -File .\aggiorna_pages.ps1 -Uninstall

  PREREQUISITI (una volta sola)
    - Python e Git installati e nel PATH.
    - La cartella del progetto e' un repository Git collegato al tuo repo GitHub
      (remote "origin") e GitHub Pages e' impostato su branch main, cartella /docs.
    - Aver fatto UN "git push" manuale, cosi' le credenziali restano memorizzate
      (i push automatici non possono chiedere password).

  OPZIONI
    -MinIntervalHours N : se l'ultimo giro e' piu' recente di N ore, salta
                          (utile per non rieseguire a ogni accesso). Default 0.
#>
[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Uninstall,
    [int]$MinIntervalHours = 0,
    [string]$TaskName = "MotoMonitorPages"
)

Set-Location -Path $PSScriptRoot
$Log = Join-Path $PSScriptRoot "pages_update.log"

function Write-Log {
    param([string]$Message)
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $Log -Value $line
    Write-Host $line
}

# ---- Installazione avvio automatico al login ----
if ($Install) {
    $psExe = (Get-Command powershell.exe).Source
    $arg = '-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $PSCommandPath
    $action = New-ScheduledTaskAction -Execute $psExe -Argument $arg -WorkingDirectory $PSScriptRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Description "Aggiorna la pagina Moto Monitor su GitHub Pages a ogni accesso a Windows." -Force | Out-Null
    Write-Host "OK: attivita' '$TaskName' registrata. Verra' eseguita a ogni accesso a Windows."
    Write-Host "Per rimuoverla: powershell -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Uninstall"
    Write-Host "Se la registrazione fallisce per permessi, esegui PowerShell come amministratore."
    exit 0
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "OK: attivita' '$TaskName' rimossa."
    exit 0
}

# ---- Esecuzione normale ----
Write-Log "=== Avvio aggiornamento pagina ==="

# Throttle opzionale: salta se l'ultimo giro e' troppo recente
if ($MinIntervalHours -gt 0) {
    $statePath = Join-Path $PSScriptRoot "state.json"
    if (Test-Path $statePath) {
        try {
            $state = Get-Content $statePath -Raw | ConvertFrom-Json
            if ($state.last_run) {
                $age = (Get-Date) - [datetime]$state.last_run
                if ($age.TotalHours -lt $MinIntervalHours) {
                    Write-Log ("Ultimo giro {0:N1} ore fa (< {1}h): salto." -f $age.TotalHours, $MinIntervalHours)
                    exit 0
                }
            }
        } catch { }
    }
}

# Individua Python (python oppure py)
$py = "python"
if (-not (Get-Command $py -ErrorAction SilentlyContinue)) {
    if (Get-Command "py" -ErrorAction SilentlyContinue) { $py = "py" }
    else { Write-Log "ERRORE: Python non trovato nel PATH."; exit 1 }
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Log "ERRORE: Git non trovato nel PATH."; exit 1
}

# Esegui il monitor (genera docs/index.html, report, aggiorna state.json)
Write-Log "Eseguo $py monitor.py ..."
& $py "monitor.py" 2>&1 | ForEach-Object { Add-Content -Path $Log -Value $_ }
if ($LASTEXITCODE -ne 0) {
    Write-Log "ERRORE: monitor.py ha restituito codice $LASTEXITCODE. Nessun push."
    exit 1
}

# Metti in stage solo i file esistenti che ci interessano
foreach ($f in @("docs/index.html", "state.json", "report.md", "report.html", "searches.json")) {
    if (Test-Path $f) { git add -- $f 2>&1 | Out-Null }
}

# Pubblica solo se c'e' davvero qualcosa in stage
$staged = git diff --cached --name-only
if ([string]::IsNullOrWhiteSpace($staged)) {
    Write-Log "Nessuna modifica da pubblicare."
    exit 0
}

$msg = "Aggiornamento pagina {0}" -f (Get-Date -Format "yyyy-MM-dd HH:mm")
git commit -m $msg 2>&1 | ForEach-Object { Add-Content -Path $Log -Value $_ }
git push 2>&1 | ForEach-Object { Add-Content -Path $Log -Value $_ }
if ($LASTEXITCODE -ne 0) {
    Write-Log "ERRORE nel push. Esegui un 'git push' manuale una volta per memorizzare le credenziali."
    exit 1
}

Write-Log "Pagina aggiornata e pubblicata su GitHub Pages."
