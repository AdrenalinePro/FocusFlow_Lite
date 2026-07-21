$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvDir = Join-Path $projectDir ".venv-integrated"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$bundledPython = "C:\Users\Lenovo\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$env:MPLCONFIGDIR = Join-Path $projectDir ".matplotlib-cache"

if (-not (Test-Path -LiteralPath $venvPython)) {
    if (Test-Path -LiteralPath $bundledPython) {
        $basePython = $bundledPython
    } else {
        $basePython = (Get-Command python -ErrorAction Stop).Source
    }
    Write-Host "Creating .venv-integrated..." -ForegroundColor Cyan
    & $basePython -m venv $venvDir
}

Write-Host "Installing Python 3.12 compatible dependencies..." -ForegroundColor Cyan
& $venvPython -m pip install --disable-pip-version-check --progress-bar off `
    "numpy>=2,<3" "bleak>=0.22,<1" "websockets==12.0" `
    "onnxruntime>=1.17,<2" "affectivecloud==1.2.9" `
    "opencv-python>=4.8" "mediapipe>=0.10" "mss>=9" `
    "Pillow>=10" "imagehash>=4.3" "requests>=2.31" `
    "PyWavelets>=1.4" "scipy>=1.10"

# EnterBLE 1.1.6 itself works with modern Bleak, but its package metadata pins
# the Python-3.10-only bleak-winrt stack. Install the pure-Python SDK without
# resolving that obsolete transitive pin.
& $venvPython -m pip install --disable-pip-version-check --no-deps "enterble==1.1.6"

& $venvPython -c "import affectivecloud, bleak, cv2, enterble, mediapipe, mss, numpy, onnxruntime, scipy, websockets; print('Integrated environment is ready.')"
if ($LASTEXITCODE -ne 0) {
    throw "Integrated environment validation failed."
}

Write-Host "Setup complete. Start with: .\run_integrated.cmd" -ForegroundColor Green
