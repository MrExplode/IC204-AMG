# ic204amg

Standalone AMG-menu job for the W204 IC204 cluster over a Tactrix Openport 2.0 (J2534).
Python, 32-bit, Windows laptop, no Vediamo.

Sequence: `10 03` (P2/P2* taken from the reply) → `27 09` / `27 0A` (in-process seed-key, SW
2049022903 level 9) → read `0x01D0` (8) + `0x0260` (10) → backup to `backups/*.json` → byte 0
FC↔FF, else stop → `3D` write → read-back verify → `11 01`. TesterPresent is a firmware periodic
message. The tool can only ever write those two windows (`uds.WRITABLE`); every PDU is logged to
`logs/`.

## Develop (Linux)

```
uv sync                                  # .venv: python 3.15 (matches the bundle)
uv run pytest
uv run ruff format . && uv run ruff check . && uv run ty check
```

## 1. CAN ids (once, on the laptop)

udsoncan needs the cluster's request/response ids and baud; they are comparameters in the CBF.
`tools/cbf/CbfComParams.cs` dumps them using CaesarSuite's `Caesar.dll` (Trafo is gone from the
release). On the laptop, in one folder: `CbfComParams.cs`, `build.cmd`, `Caesar.dll` and
`Newtonsoft.Json.dll` (from `CaesarSuite_2022-04-28.zip`, needs .NET Framework 4.6+, present),
and `IC_204_Xentry.CBF`:

```
build.cmd
CbfComParams.exe IC_204_Xentry.CBF IC_204 > ic204_comparams.txt
```
Take `CP_REQUEST_CANIDENTIFIER`, `CP_RESPONSE_CANIDENTIFIER`, `CP_BAUDRATE` of the HS-CAN
(UDS) subtype and put them in `car.toml` (copy `car.example.toml`). Keep the dump in the repo.

## 2. Bundle and deploy

```
python3 tools/build_bundle.py        # dist/py: embeddable CPython 3.15 win32 + udsoncan + this
scp -r dist/py/. <laptop>:C:/work/amg/py/
```
Nothing is installed on the laptop; delete the folder to undo. Vediamo must be closed (the
Openport is exclusive).

## 3. At the car

```
cd C:\work\amg\py
ic204amg selftest      # seed-key vectors, udsoncan, op20pt32.dll load; no cable needed
ic204amg probe         # cable: firmware/DLL version, battery voltage; no bus traffic
ic204amg readonly      # unlock + backup both windows; writes nothing
ic204amg write [--expect backups\<earlier>.json]
ic204amg lampfix       # only if ESP/CNG/preheat lamp misbehaves afterwards (runbook step 12)
ic204amg restore backups\<file>.json [--lamp]
```
Every writing command re-reads first, saves a fresh backup, refuses below 12.5 V
(`--ignore-battery` overrides) and asks you to type the new byte value. `write` toggles byte 0
each time it runs, so run it once; `lampfix` and `restore` are the follow-ups.
Exit codes: 0 ok, 1 usage/config, 2 runbook STOP, 3 UDS/transport error. Copy `backups/` and
`logs/` back here after each session.

## Layout

- `ic204amg/uds.py` request builders, write allow-list, NRC table, strict response checks
- `ic204amg/transport.py` Openport via udsoncan `J2534Connection`, firmware TesterPresent
- `ic204amg/cluster.py` procedure, backup files, byte-0 rule, lamp fix, restore
- `ic204amg/seedkey.py` Mattwmaster58/ic204 algorithm, trimmed; vectors in `VECTORS`
- `tests/` pytest; `tests/fakes.py` scripted cluster (session gate, seed-key, two windows)
- `tools/build_bundle.py`, `tools/cbf/`
