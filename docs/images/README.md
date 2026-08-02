# Images

`dashboard.png` (referenced from the root README) is not committed yet. To make
one without a car:

```sh
PYTHONPATH=python python3 -m volvo_diag serve --fake
```

Open `http://127.0.0.1:8080/`, tick a few parameters so the charts fill, and
take a screenshot (or a short GIF) into `docs/images/dashboard.png`. The
synthetic data looks the same as the real thing, so it is fine for the README.
