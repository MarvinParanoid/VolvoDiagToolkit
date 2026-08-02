# Building and trying it on Linux (no car)

Everything except the vendor driver builds and runs on Linux, against the
simulated ECM in [`fake-j2534/`](../fake-j2534/):

```sh
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build
ctest --test-dir build            # the C++ core test
pip install -e ".[dev]" && pytest # the Python suite (what CI runs)

# a J2534 session, through the proxy, into the fake ECM
export VOLVO_J2534_REAL_DLL=$PWD/build/fake-j2534/fake_j2534.so
export VOLVO_J2534_LOG_DIR=/tmp/volvo-logs
./build/test-client/j2534-test ./build/proxy/j2534proxy.so --request 22F190

# what the proxy recorded
PYTHONPATH=python python3 -m volvo_diag.logs.summarize /tmp/volvo-logs/*.jsonl

# the dashboard, reading the simulator through the same stack
PYTHONPATH=python python3 -m volvo_diag.cli \
    --transport j2534 --library ./build/proxy/j2534proxy.so \
    --definitions definitions/simulator monitor
```

The simulator's identifiers (`22 FE xx`) are **made up**. They live in
`definitions/simulator/` and are never loaded against a real car — the default
definition path is `definitions/volvo/`.

To preview the dashboard itself with no build and no adapter — synthetic data on
the real definitions — just run:

```sh
PYTHONPATH=python python3 -m volvo_diag serve --fake
```

## Cross-compiling the DLL for the Windows target

To produce the Windows `j2534proxy.dll` (and `j2534-test.exe`, `fake_j2534.dll`)
from Linux with mingw-w64 and copy them over:

```sh
./scripts/build-mingw.sh i686        # or x86_64
```

That script also checks the two things that silently break a J2534 DLL: that the
exports are undecorated (`PassThruOpen`, not `_PassThruOpen@8`) and that nothing
outside the system DLLs has to be resolved at load time — VIDA's machine has no
`libwinpthread-1.dll`. With wine installed it then runs the unit tests and a
proxy→fake-driver session as a smoke test. See
[install-windows.md](install-windows.md) for which build to use where.
