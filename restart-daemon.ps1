param(
    [string]$DataDir = (Get-Location).Path
)

# The daemon's main() handles displacing any existing instance via graceful
# shutdown + force-kill fallback, so no pre-shutdown step is needed here.
Write-Host "Starting ContextGarden daemon (data-dir: $DataDir)..."
Write-Host "Press Ctrl+C to stop."
conda run -n contextgarden python -m graph.daemon --data-dir "$DataDir"
