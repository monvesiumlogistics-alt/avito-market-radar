# Автозапуск AvitoHunter при входе в Windows. Запускать вручную: правой кнопкой -> "Выполнить с помощью PowerShell".
# Удалить автозапуск: Unregister-ScheduledTask -TaskName AvitoHunter -Confirm:$false
$bat = Join-Path $PSScriptRoot "run_bot.bat"
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$bat`"" -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT1M"  # минута на подключение VPN/сети после входа
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
# Interactive: окно браузера бота видно на рабочем столе (нужно, чтобы проходить капчу)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
Register-ScheduledTask -TaskName "AvitoHunter" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force
Write-Host "Готово: AvitoHunter будет запускаться при входе в Windows (через 1 мин)."
