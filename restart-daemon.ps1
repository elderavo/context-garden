$Port = if ($env:CG_DAEMON_PORT) { $env:CG_DAEMON_PORT } else { "7432" }

# Send daemon.shutdown RPC directly over TCP
try {
    $tcp = New-Object System.Net.Sockets.TcpClient
    $tcp.Connect("127.0.0.1", [int]$Port)
    $stream = $tcp.GetStream()
    $msg = '{"id":"1","method":"daemon.shutdown","params":{}}' + "`n"
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($msg)
    $stream.Write($bytes, 0, $bytes.Length)
    $stream.Flush()
    Start-Sleep -Milliseconds 500
    $tcp.Close()
    Write-Host "Daemon stopped."
} catch {
    Write-Host "Daemon not reachable -- skipping shutdown."
}

# Poll until port is free (up to 10s)
$deadline = (Get-Date).AddSeconds(10)
while ((Get-Date) -lt $deadline) {
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $tcp.Connect("127.0.0.1", [int]$Port)
        $tcp.Close()
        Start-Sleep -Milliseconds 300
    } catch {
        break  # Port is free
    }
}

Write-Host "Starting daemon on port $Port..."
conda run -n contextgarden python -m graph.daemon
