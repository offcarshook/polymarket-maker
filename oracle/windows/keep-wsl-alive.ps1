# Keeps the WSL Ubuntu VM (and the paper bot's systemd services) running after you
# close the terminal, and starts it automatically when you log in to Windows.
# Run once in PowerShell:  powershell -ExecutionPolicy Bypass -File keep-wsl-alive.ps1
# Remove later:            Unregister-ScheduledTask -TaskName PolymakerWSL -Confirm:$false
$distro = "Ubuntu"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-WindowStyle Hidden -NoProfile -Command wsl.exe -d $distro --exec sleep infinity"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "PolymakerWSL" -Action $action -Trigger $trigger -Settings $settings `
  -Description "Keep WSL running for the polymaker paper test" -Force | Out-Null
Start-ScheduledTask -TaskName "PolymakerWSL"
Write-Host "PolymakerWSL task registered and started. WSL will stay up and start at every login."
