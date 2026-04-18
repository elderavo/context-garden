param(
    [switch]$Force
)

$ports = @(7432, 7433)
$killed = @()

# Step 1: Kill by port (daemon TCP + HTTP)
foreach ($port in $ports) {
    $connections = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
    foreach ($conn in $connections) {
        $pid_ = $conn.OwningProcess
        if ($pid_ -and $pid_ -notin $killed) {
            $proc = Get-Process -Id $pid_ -ErrorAction SilentlyContinue
            if ($proc) {
                Write-Host "Killing PID $pid_ ($($proc.ProcessName)) on port $port"
                Stop-Process -Id $pid_ -Force
                $killed += $pid_
            }
        }
    }
}

# Step 2: Kill any node.exe processes running context-garden (the MCP server
# that auto-respawns the daemon via DaemonClient). Without this, the daemon
# reappears immediately after step 1.
$nodeProcs = Get-CimInstance Win32_Process -Filter "Name = 'node.exe'" -ErrorAction SilentlyContinue
foreach ($p in $nodeProcs) {
    if ($p.CommandLine -match "context-garden|context_garden") {
        $pid_ = $p.ProcessId
        if ($pid_ -notin $killed) {
            $proc = Get-Process -Id $pid_ -ErrorAction SilentlyContinue
            if ($proc) {
                Write-Host "Killing PID $pid_ (node - MCP server)"
                Stop-Process -Id $pid_ -Force
                $killed += $pid_
            }
        }
    }
}

# Step 3: Verify ports are actually free
Start-Sleep -Milliseconds 500
foreach ($port in $ports) {
    $still = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
    if ($still) {
        $pid_ = ($still | Select-Object -First 1).OwningProcess
        Write-Warning "Port $port still in use by PID $pid_ - trying harder"
        taskkill /PID $pid_ /T /F 2>$null | Out-Null
    }
}

if ($killed.Count -eq 0) {
    Write-Host "No ContextGarden processes found on ports $($ports -join ', ')."
} else {
    Write-Host "Done. Killed $($killed.Count) process(es)."
}
