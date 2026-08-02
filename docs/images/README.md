# Images

Dashboard screenshots used by the docs, generated from **synthetic data** (no car):

| file | view |
| --- | --- |
| `dashboard.png` | Live — selected parameters with time-series charts |
| `dashboard-config.png` | Configuration — vehicle identity + car configuration |
| `dashboard-codes.png` | Codes — trouble-code sweep (shows the demo `2A30`) |

Regenerate them all with one command:

```sh
pip install playwright          # once; also: playwright install chromium
python scripts/screenshots.py
```

The script spawns `serve --fake`, drives a headless browser (dark theme, 2x)
through the three views and overwrites the PNGs. The synthetic data looks the
same as the real thing, so the shots are representative.
