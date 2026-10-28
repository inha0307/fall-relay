# Fall-Relay time sync for the server PC (Windows + WSL).
# Run in an elevated PowerShell (Run as administrator), giving the LAN time server (the edge device running chrony):
#   powershell -ExecutionPolicy Bypass -File .\fall-relay_time_sync.ps1 -TimeServer <edge-device-ip>
#
# 1. Windows follows the LAN time server every 15 minutes.
# 2. WSL keeps no NTP client of its own: Hyper-V keeps the WSL clock on Windows time.
#    Two clients pulling towards different times made the WSL clock run ~8 % fast.

param([Parameter(Mandatory = $true)][string]$TimeServer)

sc.exe config w32time start= auto | Out-Null
Start-Service w32time -ErrorAction SilentlyContinue
w32tm /config /manualpeerlist:"$TimeServer,0x9" /syncfromflags:manual /reliable:no /update
reg add HKLM\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\NtpClient /v SpecialPollInterval /t REG_DWORD /d 900 /f | Out-Null
Restart-Service w32time
w32tm /resync /force
Write-Host "`n--- Windows time status ---"
w32tm /query /status
Write-Host "`n--- Offset to the time server (5 samples) ---"
w32tm /stripchart /computer:$TimeServer /samples:5 /dataonly

Write-Host "`n--- WSL: disable the guest NTP client ---"
wsl -u root systemctl disable --now systemd-timesyncd
wsl -u root systemctl is-active systemd-timesyncd
