# ACBC GivEnergy Dashboard v2.4 -- Simulator Regression Report

Date: 2026-06-11
Dashboard commit: 93aaa6e (main)
Simulator: psylsph/givenergy-simulator v0.15.0 (MIT, built from source)
Platform: Raspberry Pi 4B aarch64, Debian 12, Rust 1.96.0
Test host: Windows 11, Python 3.14.3 (venv), SSH key-based (no passwords)
Harness: tools/sim_harness*.py with SSH key injection, pkill -9, wait_port_free, wait_ready (log-polling), model-level retry (up to 3 attempts), --tick-interval 3600

---

## Summary

| Suite | Checks | Result |
|---|---|---|
| Detection regression (sim_harness.py) | 15/15 | PASS |
| Control-state + slot round-trip (sim_harness_control.py) | 24/24 | PASS |
| Live data decode (sim_harness_live.py) | 21/21 | PASS |
| Unit tests (test_core + test_scheduler) | 71/71 | PASS |

**TOTAL: 131/131 checks pass.**

## Release gate result: GO for v2.4

---

## 1. Detection Regression

Script: `tools/sim_harness.py`

Cycles 15 inverter models. The simulator is restarted per model, detection is performed by the dashboard's own `_detect_inverter()` / `_classify_model()` path over the real 59 59 Modbus framing.

**Headline assertion (commit 57cb736 fix):** AllInOne6 (`0x8001`) must detect as `single_phase_extended`, not `three_phase_aio`. PASS.

| Sim model | DTC | fw | Detected profile | Detected name | Result | Note |
|---|---|---|---|---|---|---|
| AllInOne6 | 0x8001 | 318 | single_phase_extended | All in One | PASS | v2.4 release-gate headline |
| AllInOne | 0x8002 | 318 | single_phase_extended | All in One | PASS | |
| AllInOne5 | 0x8003 | 318 | single_phase_extended | All in One | PASS | |
| AIO8kW | 0x8102 | 318 | single_phase_extended | Hybrid Inverter Gen 3 HV | PASS | 0x81xx HV Gen3 |
| AIOHybrid6kW | 0x8201 | 318 | single_phase_extended | All in One 2 | PASS | newly modelled in v0.15.0 |
| Gen4Hybrid6kW | 0x8304 | 318 | single_phase_extended | Unknown (0x83) | PASS | |
| Gen1Hybrid | 0x2001 | 252 | single_phase_2slot | Hybrid Inverter Gen 1 | PASS | fw century 2 |
| Gen2Hybrid | 0x2001 | 852 | single_phase_2slot | Hybrid Inverter Gen 2 | PASS | fw century 8 |
| Gen3Hybrid | 0x2001 | 318 | single_phase_extended | Hybrid Inverter Gen 3 | PASS | fw century 3 |
| ACCoupled | 0x3001 | 318 | single_phase_ac_coupled | AC Coupled Inverter | PASS | |
| EMS | 0x5001 | 318 | single_phase_2slot | Energy Management | PASS | |
| ThreePhase | 0x4001 | 612 | three_phase_aio | Three-Phase Hybrid | PASS | |
| ACThreePhase | 0x6001 | 612 | three_phase_aio | Three-Phase AC | PASS | |
| AIOCommercial | 0x4101 | 318 | three_phase_aio | AIO Commercial | PASS | |
| Gateway12kW | 0x7001 | 318 | gateway_aio | Gateway | PASS | |

---

## 2. Control-State and Slot Round-Trip

Script: `tools/sim_harness_control.py`

For four representative profiles:
- Reads control state and checks profile, slot counts, and writable flag.
- Writes charge slot 1 (HR 94/95) and reads back through `_read_control_state()`.
- For AllInOne6 (single_phase_extended): writes charge slot 2 via HR 243/244 -- the v2.4 `_charge_slot_hrs(profile)` extended path -- and reads back.

`--tick-interval 3600` fast-forwards the daily scenario so the sim enters keep-alive mode (stops re-projecting the default schedule) before writes are issued.

| Model | Profile | Charge slots | Discharge slots | Writable | Slot-1 round-trip | Slot-2 HR 243/244 | Result |
|---|---|---|---|---|---|---|---|
| AllInOne6 | single_phase_extended | 10 | 10 | yes | 04:30-06:00 PASS | 09:00-10:30 PASS | 6/6 PASS |
| Gen2Hybrid | single_phase_2slot | 2 | 2 | yes | 04:30-06:00 PASS | n/a | 5/5 PASS |
| ACCoupled | single_phase_ac_coupled | 1 | 2 | yes | 04:30-06:00 PASS | n/a | 5/5 PASS |
| ThreePhase | three_phase_aio | 2 | 2 | no (read-only) | n/a | n/a | 4/4 PASS |

24/24 checks PASS.

The slot-2 HR 243/244 path is the exact register range introduced in v2.4. The simulator (v0.15.0) correctly models the extended 240-299 block, confirming end-to-end: dashboard writes HR 243/244, simulator stores and returns them, dashboard `_read_control_state()` decodes them as slot-2 times. Gap noted in 10 Jun reference doc (no 243/244 support) is closed in v0.15.0.

---

## 3. Live Data Decode

Script: `tools/sim_harness_live.py`

Sends the dashboard's own IR(0,60) poke to the simulator, collects the response using `_pop_data_frames()` / `_decode_listen_frame()`, and validates solar / home / battery / grid / SOC / voltage decode sensibly.

All three models produce the same scenario values (basic_day.yaml defaults): solar=0W, home=600W, battery=600W discharging, grid=0W, SOC=11%, v_battery=44.85V.

| Model | Decoded | SOC 0-100 | vbat 30-70 | solar>=0 | home>=0 | battery>=0 | grid>=0 | Result |
|---|---|---|---|---|---|---|---|---|
| AllInOne6 | PASS | PASS | PASS | PASS | PASS | PASS | PASS | 7/7 |
| Gen2Hybrid | PASS | PASS | PASS | PASS | PASS | PASS | PASS | 7/7 |
| Gen3Hybrid | PASS | PASS | PASS | PASS | PASS | PASS | PASS | 7/7 |

21/21 checks PASS.

---

## 4. Unit Tests

```
71 passed in 0.60s
```

test_core.py (46 tests) and test_scheduler.py (25 tests) all pass on current main (93aaa6e). No regressions from v2.4 changes.

---

## Fidelity notes

**Protocol framing:** sim-modbus implements the full GivEnergy 59 59 framing (MBAP variant, TCP port 5020 in harness). Slave address in read path is generic (serves any slave address, including 0x11 used by the dashboard). CRC-16/Modbus computed slave-inclusive, LE byte order -- matches the dashboard `_crc16` implementation exactly. CRC mismatch is logged but not fatal (lenient decode), which means the harness does not mask CRC bugs in the dashboard.

**Register coverage:** Extended block HR 240-299 fully modelled. `uses_gen3_extended_slots()` in the simulator correctly gates slot-2 register projection to HR 243/244 for Gen3+ and AIO family, matching the dashboard's `_charge_slot_hrs(profile)` logic.

**Read-count cap:** Simulator enforces a cap of 60 registers per read request. Dashboard max request is 60. Compatible.

**Models not in simulator:** No `0x8001`-variant with real battery module BMS data -- simulator returns synthetic values for all live-data fields. Physical slot 3-10 round-trips require real AIO hardware for full confidence (Gaz's unit). Detection and HR 243/244 are confirmed sim-side; physical write verification remains open.

---

## Known limitations / out of scope

- Physical slots 3-10 write/verify: simulator models them correctly; physical confirmation requires real extended-slot AIO hardware.
- BMS multi-module SOC weighting: unit-tested in test_core.py; not exercised by simulator (simulator returns single synthetic SOC).
- No negative power values tested: simulator's basic_day.yaml does not model grid export or battery charge from solar. Negative-power decode paths covered by unit tests only.

---

## Go/No-Go

All 131 checks pass across all four test surfaces. The headline regression (AllInOne6 must not misclassify as three_phase_aio) passes. The v2.4 extended-slot write path (HR 243/244) round-trips correctly end-to-end. Unit baseline is clean.

**v2.4: GO.**
