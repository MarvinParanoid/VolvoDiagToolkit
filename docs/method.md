# Finding a parameter

The proxy turns VIDA into a reference implementation: whatever VIDA can show
on screen, it asked the car for, and the log has the question and the answer.
The job is to work out which question produced which number.

## Before anything else

1. `scripts\list-j2534.ps1` — write down the bitness of the vendor DLL and of
   VIDA itself. Build the proxy to match (`build-windows.ps1 -Arch Win32` with
   MSVC, or `./scripts/build-mingw.sh i686` cross-compiling from Linux). This
   is the single most common reason nothing works, and the failure looks like
   "device not found" rather than anything about bitness.
2. Verify the proxy is transparent: connect to the car through it and read the
   VIN in VIDA. If VIDA behaves any differently than with the vendor driver
   directly, stop and fix that first. A proxy that changes behaviour makes
   every later observation worthless.
3. `python -m volvo_diag.logs.summarize <log>` on that first session. You
   should see the channels, the flow control filters and a `22 F1 90`
   exchange. Now the pipeline is trustworthy.

## One parameter per recording

The whole method is a controlled experiment, so it is worth being pedantic:

| step | why |
| --- | --- |
| same car, same ignition state, engine at the same idle | the values must move only because the parameter changed |
| same base set of parameters selected in VIDA | so the diff is a single line |
| add exactly one new parameter | two at a time and you cannot tell which request is which |
| record 20–30 seconds | enough samples to see the polling rate |
| set `session_tag` per recording | the tag ends up in the file name |

Suggested sequence — the file names the tooling expects:

```
00-identification.jsonl      connect, read VIN, read ECM software number
01-baseline-rpm.jsonl        engine speed only
02-plus-boost.jsonl          + boost actual
03-plus-boost-requested.jsonl
04-plus-dpf-pressure.jsonl
05-plus-dpf-load.jsonl
06-plus-egt.jsonl
07-plus-regeneration.jsonl
```

Then:

```
python -m volvo_diag.logs.diff 01-baseline-rpm.jsonl 02-plus-boost.jsonl
```

## Reading the diff

Three outcomes, all of them normal:

**A new request appeared.** The easy case:

```
only in 02-plus-boost.jsonl:
  CAN 0x7E0  22 D1 23        426x  resp len 5   62D12303F6
```

**No new request, but a response got longer.** VIDA groups parameters into one
request when it can. The diff reports this as *same request, different shape*.
The new value is somewhere in the extra bytes — use `--track` and correlate
with what VIDA displays.

**No difference at all.** Either the parameter is computed by VIDA from data it
already had, or it comes from a different ECU on a channel you did not expect.
Check whether a second channel was opened.

## From bytes to a number

```
python -m volvo_diag.logs.summarize 02-plus-boost.jsonl --track 22D123 --track-csv boost.csv
```

This prints every answer over time with the plausible readings side by side:

```
t=  12.480  03F6   u8=3  i8=3  u16=1014  u16*0.1=101.4  i16=1014
```

Now match it against the number VIDA displayed at the same moment. Two
independent samples at clearly different values settle the scale, and the
offset falls out of them:

```
scale  = (raw2 - raw1) / (vida2 - vida1)
offset = vida1 - raw1 / scale
```

Sanity checks that catch most mistakes:

* absolute boost at idle is roughly barometric pressure — compare against
  standard PID 0x33;
* DPF differential pressure at idle is under a few kPa;
* exhaust temperature must be plausible against coolant temperature at a cold
  start (both near ambient);
* a percentage that goes above 100 or below 0 means the wrong encoding.

## Writing it down

Add the parameter to `definitions/volvo/p1/d4164t.yaml` with `status:
discovered` and both the source log and a raw `sample`. Promote it only after
it survives a full drive:

* `discovered` — the car answers, encoding is a guess
* `experimental` — the value is stable and plausible over a drive
* `verified` — cross-checked against physics or a second parameter
* `verified-against-vida` — VIDA and `volvo-monitor` show the same number at
  the same time

Record the ECM software number (`22 F1 94`) with it. The same identifier on
another software level may be scaled differently, and a definition without a
software level attached is a trap for the next person — probably you.

## What not to do yet

No writes. Not `2E`, not `31`, not `27`, not `2F`, not clearing adaptations,
not forcing a regeneration. The proxy makes it obvious how VIDA does those
things, which is exactly what makes it tempting. Reading is reversible;
everything else is not, and a half-verified parameter database is not the
foundation to build a write on.

## Other modules (stage 7)

Before assuming the OBD connector reaches every module, record VIDA talking to
CEM, DIM, ABS, PAM and check the log for:

* a second `connect` — a new channel, possibly at a different baud rate;
* different CAN ids in the flow control filters;
* vendor-specific ioctls between the two conversations;
* whether the adapter physically switches lines.

`summarize` prints all four in its header, which is why it prints the channel
and filter tables before anything else.
