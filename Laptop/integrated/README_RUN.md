# FocusFlow EEG module — UNO Q run guide

## 1. Files that must be uploaded together

- `eeg_reader.py`
- `dashboard.html`
- `chart.umd.min.js`
- `requirements.txt`

Keep all four files in `/home/arduino/workshop4/`.

## 2. Check the UNO Q environment

```bash
cd /home/arduino/workshop4
python3 --version
python3 -m pip show affectivecloud enterble bleak websockets
```

Only if a dependency is missing or its version is wrong:

```bash
python3 -m pip install -r requirements.txt
```

## 3. Set credentials in the current terminal

Do not put real credentials in source files or screenshots.

```bash
export APP_KEY="your_app_key"
export APP_SECRET="your_app_secret"
export CLIENT_ID="focusflow_user_001"
```

Optional settings:

```bash
export WS_HOST="0.0.0.0"
export WS_PORT="8765"
```

## 4. Start the dashboard HTTP server

Open the first SSH terminal:

```bash
cd /home/arduino/workshop4
python3 -m http.server 8000 --bind 0.0.0.0
```

On the laptop, open:

```text
http://UNO_Q_IP:8000/dashboard.html
```

For example, if the UNO Q address is `10.162.212.68`, use
`http://10.162.212.68:8000/dashboard.html`.

## 5. Start EEG collection

Open a second SSH terminal, set the three credential variables again, then run:

```bash
cd /home/arduino/workshop4
python3 eeg_reader.py
```

The program creates `session_YYYYMMDD_HHMMSS.jsonl`. Each line is an independent
record, so an unexpected power loss does not invalidate the whole session.

## 6. Acceptance check

1. The page changes from “waiting” to “connected”.
2. Battery and wear state are visible.
3. Left/right EEG waveforms move and the time axis shows a five-second window.
4. Band power and the official attention reference update.
5. Removing the headband clears the affective gauges.
6. Clicking Rest/Focus/Distraction/End adds marker records to the JSONL file.
7. Turn the headband off, wait, then turn it on; the terminal should report a
   rescan and reconnection without restarting the Python program.
8. Press `Ctrl+C`; the program should close the cloud session and leave a
   readable JSONL file.

## Laptop context integration

First-time setup (or after moving this folder to another computer):

```powershell
.\setup_integrated.cmd
```

The setup creates `.venv-integrated`; it does not depend on the absolute path
stored inside an older copied virtual environment.

For the complete Windows monitor, set the three cloud credentials plus the
screen-recognition key, then run the single launcher:

```powershell
$env:APP_KEY="..."
$env:APP_SECRET="..."
$env:CLIENT_ID="focusflow_user_001"
$env:MINIMAX_API_KEY="..."
.\run_integrated.cmd
```

The launcher now connects to BLE device `UNO-Q-FF01` after the Flowtime link is
ready and forwards one `decision_update` per second. To avoid Windows adapters
that cannot scan reliably during an existing GATT notification stream, startup
first scans and caches the UNO Q `BLEDevice`, then connects Flowtime, and finally
connects the cached UNO Q without another scan. Keep UNO Q powered and advertising
before starting the launcher. Use a different advertised name/address or disable
the bridge during laptop-only debugging with:

```powershell
.\run_integrated.cmd --uno-device "UNO-Q-FF01"
.\run_integrated.cmd --no-uno
```

The UNO Q receiver addition is specified in `UNO_Q_BLE_DECISION_PROTOCOL.md`.

It opens `dashboard.html` and starts the GitHub camera module, 30-second screen
monitor, Flowtime BLE/cloud collector and hierarchical decision engine in one
process. Press `Ctrl+C` once and wait for the PowerShell prompt so all four
resources are released in order.

The complete runner also serves a compact laptop display at
`http://127.0.0.1:8000/focusflow_mini.html`. Open it automatically with:

```powershell
.\run_integrated.ps1 --display mini
```

Both pages share the same authoritative rest timer. Choose a duration and click
“开始休息”. During rest the camera and screen-capture/API workers stop, while EEG
BLE/cloud transport remains connected for stability but waveform, heart-rate,
feature, prediction and decision outputs are muted. At zero (or after “结束休息”)
the camera/screen workers restart and the decision engine waits for fresh input
instead of reusing pre-rest state.

The final user-facing state is hierarchical. Send the screen result every
30-60 seconds and the camera result continuously to the existing dashboard
WebSocket (`ws://127.0.0.1:8765` when everything runs on the laptop):

```json
{"type":"screen_state","state":"专注工作","is_learning":true,"confidence":0.91}
{"type":"camera_state","face_detected":true,"state":"专注","is_focused":true,"state_duration":8.2,"confidence":0.9}
```

`is_learning` should be supplied by the screen-content classifier whenever
possible. The final priority is fixed: non-learning screen = `摸鱼`; learning
screen plus absent/diverted user for at least 3 seconds = `走神`; only after
both gates pass is the current EEG percentage displayed as `专注：xx%`.

## 7. Playback

Choose Playback in the dashboard and select either an old `.json` recording or
a new `.jsonl` recording. A partial final JSONL line caused by a sudden shutdown
is ignored automatically.
