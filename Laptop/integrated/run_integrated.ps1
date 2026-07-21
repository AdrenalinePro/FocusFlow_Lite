$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectDir ".venv-integrated\Scripts\python.exe"
$envFile = Join-Path $projectDir ".env.ps1"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Missing integrated environment. Run .\setup_integrated.ps1 once."
}

# Keep Matplotlib's first-run cache inside the writable project instead of the
# user profile, which may be read-only in managed environments.
$env:MPLCONFIGDIR = Join-Path $projectDir ".matplotlib-cache"

& $python -c "import affectivecloud, bleak, cv2, enterble, mediapipe, mss, numpy, onnxruntime, websockets"
if ($LASTEXITCODE -ne 0) {
    throw "Integrated Python environment is incomplete. Run .\setup_integrated.ps1 again."
}

# Load previously saved cloud credentials when the current shell has none.
if ((-not $env:APP_KEY -or -not $env:APP_SECRET) -and (Test-Path -LiteralPath $envFile)) {
    . $envFile
}

if (-not $env:APP_KEY -or -not $env:APP_SECRET) {
    throw "APP_KEY and APP_SECRET are not configured."
}

if (-not $env:MINIMAX_API_KEY) {
    $laptopDir = Split-Path -Parent $projectDir
    $keyFile = Join-Path $laptopDir "apikey.txt"
    if (Test-Path -LiteralPath $keyFile) {
        $env:MINIMAX_API_KEY = (Get-Content -LiteralPath $keyFile -Raw).Trim()
    } elseif (-not (Test-Path -LiteralPath $keyFile)) {
        throw "Missing Laptop\apikey.txt. Save the MiniMax key there or set MINIMAX_API_KEY."
    }
}

Set-Location -LiteralPath $projectDir
& $python ".\run_pc_integrated.py" @args
