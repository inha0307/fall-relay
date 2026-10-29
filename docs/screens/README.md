# Web application screens

Captures of the web application running the hardware-free example (`server/bridge/demo.py --serve` with
`verify_web_demo.py --serve`), 1280 x 820 CSS pixels at 2x. All values, alerts, devices and the clip come from the
synthetic fixture; no real person or camera image is shown. These are the readable originals of Fig. 5 of the
article, captured from one v1.0.2 run. The manual-review alert comes from `edge/examples/any_detector_adapter.py`
run once against an unreachable server and then against the demo bridge (camera 3 added by the administrator
API): the queued event arrives without a usable clock calibration, so the bridge classifies it for manual review.

| File | Screen |
|---|---|
| `skeleton_view.png` | Skeleton view: the 13-joint skeletons the two synthetic cameras send (no video stream) |
| `alerts.png` | Alerts: a manual-review alert (camera 3) and a resolved live fall alert with its clip and handling timeline (camera 1) |
| `device_detail.png` | Device detail: health judged against the device profile (decision rate below the design rate) |
| `reports.png` | Reports: alerts, acknowledgement time and outcomes over a period |
