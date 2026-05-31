# ACBC GivEnergy Dashboard — Backlog (post-v1.5)

Ideas parked for future versions. Not committed work — a place to capture
context so we can pick each up cleanly later.

---

## 1. Library-free inverter control
**Goal:** Remove the `givenergy-modbus` Python dependency entirely.

**Why:**
- It pins `click==8.0.1`, which conflicts with modern Flask and produces a
  scary (but harmless) pip warning on every install.
- It's the last thing forcing a specific library/Python combination.
- We already read live data with **zero** library (raw-socket poke-and-listen),
  so the architecture is proven.

**What's needed:**
- **Holding-register reads** (for the Control page's "current state"): same
  approach as the input-register listen — send a read request, decode the
  response frame ourselves. We already decode the framing.
- **Holding-register writes** (to apply control commands): derive the *write*
  request frame the library sends (capture it the same way we captured the read
  poke: monkeypatch `socket.sendall`), then hardcode/parametrise it. Need the
  CRC handling for variable register/value (the read poke had a fixed CRC; writes
  vary, so we may need to compute CRC16-MODBUS ourselves or template it).

**Notes:** Once done, drop `givenergy-modbus` from `setup.sh`, `update.sh`,
`installer.iss`. The `_LIB`/`_fetch_v0`/`_fetch_v2`/poll-mode shim could then be
retired too (listen mode is universal).

---

## 2. Gen3 inverter control
**Goal:** Make the Control page (charge/discharge enable, slots, SOC targets,
power limits, modes) work on **Gen3 / HV hybrid** inverters, not just Gen2.

**Context:**
- Gen2 control works today via the library on slave **0x32**.
- Gen3 answers on slave **0x11** (this is what broke Gen3 *reading* until we
  added the dual-poke — the same fix applies to control).
- The library control path fails on Gen3 for the same reason reads did.

**Approach:** Best done **together with #1** — implement library-free control
with **both slave addresses** (0x32 + 0x11), exactly like the dual-poke we use
for reads. That covers Gen2 and Gen3 in one path.

**Test asset:** We have a willing Gen3 tester (Brendon / SH4DOWSIX) and the
`gen3-capture` tool to capture write frames if needed.

---

## 3. Smart Scheduler (app-held, not inverter slots)
**Goal:** A flexible scheduling/automation system **held and actioned by the
app**, independent of the inverter's two built-in charge/discharge slots.

**Why app-held instead of inverter slots:**
- The inverter only has 2 charge + 2 discharge slots — limiting.
- App-held rules can be unlimited, conditional, and smarter than the hardware.

**Possible capabilities:**
- Multiple time-based rules (e.g. "force charge 00:30–04:30", "force discharge
  16:00–19:00").
- **Conditional logic:** only act if SOC </> X%, only charge if cheap-rate, only
  discharge if SOC above reserve, skip if solar is already covering the home.
- Future: tie into an electricity price feed (Agile/Octopus) or a solar-forecast
  so it charges when cheapest / discharges at peak.

**What's needed:**
- A `schedules` table in the DB (rule definition: action, time window, days,
  conditions, enabled).
- A **scheduler thread** that wakes ~every minute, evaluates active rules against
  the live data (SOC, solar, time), and issues control commands when a rule fires
  / ends (and reverts when the window closes).
- Idempotency + logging: each action goes to the existing Activity Log; don't
  re-send a command that's already applied.
- A **UI** to create/edit/enable rules (a new panel, similar to Control).
- Depends on reliable control (#1/#2) being in place first — especially Gen3.

**Safety:** Confirm-on-apply, an easy global "pause scheduler" switch, and clear
logging so the user always knows why the inverter changed.

---

## Smaller / parked
- Temperature trend view on the hourly page (battery & inverter temps are now
  logged — the data is accumulating, just not charted yet).
- Optional: validate the power-limit %→W scaling across inverter models (the
  "50% = max" assumption) and add a one-tap "calibrate" helper.
