# Restaurant Native Print IoT Box Runtime

This is a local copy of your IoT Box runtime for the restaurant instance.

This project is now script-start only.
Do not use or regenerate packaged `.exe` builds from this copy.

What is changed:
- The web UI in `web/` is refactored.
- Local runtime configuration is reset and isolated.
- The runtime title is renamed for easier identification.
- The runtime is trimmed for cloud ESC/POS printing only.

Core routes kept:
- `/iot_drivers/action`
- `/iot_drivers/event`
- cloud websocket bridge
- ESC/POS printer queue and raw printing

Run example:

```powershell
python run_https.py
```

HTTP run example:

```powershell
python run_http.py
```

The HTTP service defaults to `http://127.0.0.1:8399` and uses its own `runtime_config_http.json`.
The main `runtime_config.json` is reserved for HTTPS on `https://127.0.0.1:8398`.

Detailed developer diagnostics are written to `logs/dev_debug.log`.

If you want this copy to use a different config path, set `IOT_CONFIG_PATH` before starting.
