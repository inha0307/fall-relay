# Installing the edge runtime on a device

Tested on Raspberry Pi 5 (Debian 13, system Python 3.13) and Raspberry Pi 4 (Debian 12, Python 3.11 virtual
environment) with 64-bit Raspberry Pi OS and a CSI camera. The reference devices are described in
[../ntu_benchmark/README.md](../ntu_benchmark/README.md).

## 1. Device packages

```bash
sudo apt install python3-picamera2            # Picamera2 and libcamera (system packages)
python3 -m venv ~/fall-relay-venv && ~/fall-relay-venv/bin/pip install -r requirements-edge.txt
```

`edge/requirements-edge.txt` pins NCNN, LiteRT (`ai-edge-litert`), OpenCV, NumPy and Requests. The runtime adds
`/usr/lib/python3/dist-packages` (and any other folder in `edge_config.json` `"extra_python_paths"`) so a virtual
environment can import the system Picamera2.

## 2. Build a bundle (on the server)

The model files come from training or a permitted copy; see [NOTICE](../../NOTICE).

```bash
python edge/make_bundle.py --out /tmp/bundle --bundle-id fall-relay-v2 \
    --lpn lpn13_fp32.tflite --tcn event_tcn_v2_fp32.tflite --clips \
    --install-dir /home/<user>/fall-relay --python /home/<user>/fall-relay-venv/bin/python --camera 1
```

The bundle contains the runtime, every module it imports (`edge/test_bundle.py` checks this), the two model files, a
`bundle_manifest.json` with their hashes and the systemd unit `fall-relay-edge-cam<N>.service`. `--clips` uploads a
short clip around each detected fall; leave it out to keep every camera image on the device.

## 3. Install

Copy the bundle to `--install-dir` on the device and add:

| File | Content |
|---|---|
| `edge_config.json` | From [edge_config.example.json](edge_config.example.json): `server_url`, `camera_id`, camera settings |
| `camera_token_cam<N>.env` | The line `FALLRELAY_CAMERA_TOKEN=...` issued on the web's Server page; mode 600 |
| `yolo26n-pose_ncnn_model/` | The YOLO26n-pose NCNN export (`model.ncnn.param`, `model.ncnn.bin`) named in `pose_model_dir` |
| camera tuning file | Only for sensors without autofocus tuning, e.g. [../camera_tuning/imx519_af.json](../camera_tuning/imx519_af.json) (`camera_tuning_file`) |

```bash
cp fall-relay-edge-cam1.service ~/.config/systemd/user/ && systemctl --user daemon-reload
systemctl --user enable --now fall-relay-edge-cam1.service
loginctl enable-linger "$USER"   # keep it running after logout
```

The runtime refuses to start when a bundled file or the pose model differs from `bundle_manifest.json`. It logs to
`edge_runtime.log` in the install folder and keeps its outbox in `edge_events.sqlite3` there.

## 4. Device agent (telemetry and remote actions)

```bash
PI_PYTHON=/home/<user>/fall-relay-venv/bin/python \
  bash server/bridge/install_pi_hardware_service.sh /home/<user>/fall-relay pi_hardware_config.json
```

with a configuration based on [pi_hardware_config.example.json](pi_hardware_config.example.json). This installs
`fall-relay-device-agent.service`, which reads the same camera token file and reports CPU load, temperature and
throttling to the bridge, where the device page shows them and queues opt-in remote actions.

## Reaching the server

`server_url` points at the bridge. In the reference setup the server opens a reverse SSH tunnel to each device
(`RPI_TARGETS` in the server's `deploy.env`), so the device uses `http://127.0.0.1:15001`. One-way device-to-server latency
measurements need synchronised clocks: in the reference setup the Pi 5 runs chrony against a public pool and serves
the LAN, the Pi 4 follows it and the server follows it every 15 min (configurations in [../time_sync](../time_sync)).

## Updating

Build the new bundle, install it into a new folder next to the current one, stop the two units, move the folders,
carry over `edge_events.sqlite3` (pending messages) and `decision_logs/`, and start the units again; keep the previous
folder for rollback.
