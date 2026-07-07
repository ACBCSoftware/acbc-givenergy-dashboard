# ACBC GivEnergy Dashboard — Backlog

Ideas, bugs, and future features. Local only — not in the repo.
Tick items off here as they ship; add new discoveries at the bottom.

---

## v1.6 — SHIPPED 01 Jun 2026

### ✅ Gen3 / AIO real-time polling — CRC fix
Root cause of all Gen3/AIO failures: CRC in Modbus requests must include
the slave byte. Gen3/AIO silently discards frames with incorrect CRC.
Fixed via wire capture comparison with GivTCP. Gen3 now responds in ~250ms.

### ✅ Heartbeat acknowledgement
Dongle sends heartbeat every ~3 min. Gen2 listen mode: do NOT ack (causes drops).
Connection now stable on all models.

### ✅ History navigation (one period at a time)
Week/Month/Year views show one period at a time with ‹ › arrows.

---

## v1.7 — SHIPPED 03 Jun 2026

### ✅ Library-free inverter control
Removed `givenergy-modbus` dependency from the control path entirely.
`_hr_read()` / `_hr_write()` over raw socket with slave-inclusive CRC.

### ✅ Inverter auto-detection + profiles
`_detect_inverter()` reads HR[0] (DTC) + HR[21] (ARM fw), classifies into
`single_phase_2slot` / `single_phase_extended` (Gen3, 10 slots) /
`three_phase_aio` / `gateway_aio`. Friendly model names shown on control page.

### ✅ Gen3 10-slot control
Gen3 / HV Gen3 expose 10 charge + 10 discharge slots with per-slot SOC targets.
Dynamic slot rendering in the UI from the API arrays.

### ✅ Gateway AIO monitoring (DTC 0x70xx)
Live data decoded from IR base=1600 (p_pv, p_load, p_ac1, p_liberty), SOC from
IR base=1780 (IR1801). Confirmed against GivTCP gateway.py + David's wire capture.
Currently passive-listen only (every ~5 min cloud sync). See v1.8 for 10s polling.

### ✅ Update notification
Polls GitHub releases API once/day; amber banner links to release-notes page.
Opt-out in config + settings. 30s deferred startup check.

### ✅ Detection on listen socket (no second connection)
Fixed Brendon's 75s-drop bug: `_detect_on_socket()` reads HR on the existing
listen socket instead of opening a second TCP connection (which the dongle —
a single-client server — could not handle).

### ✅ macOS installer
setup-mac.command + launchd LaunchAgent. User-space install, no sudo.
Not yet verified on a real Mac (needs a Mac tester).

---

## v1.9 — SHIPPED ✅ (06 Jun 2026, tag f3bb922, all platforms)

### ✅ Power time-series graph (GivEnergy-style chart)
Replicates the official GivEnergy daily power chart: signed area chart (Watts, per-minute),
toggled "Bars | Power" inside the hourly panel. Negative = cost (battery charging / grid import),
positive = benefit (discharge / export). Crosshair scrub tooltip, pinch/scroll zoom + pan,
double-tap reset, Catmull-Rom smooth curves, "now" marker, SOC on right axis. Separate
configurable colours from the bar chart (Settings → "Power Graph Colours"). New endpoint
`GET /api/power?day=YYYY-MM-DD`. **UAT passed.** Full detail in PROJECT_MEMORY.md §16.
Commits c6a11de…620df53 (13). NOTE: orientation-lock attempts were tried then fully reverted
to v1.8 behaviour (Andi disliked forced rotation once pinch-zoom worked) — do not re-add.

---

## v1.8 — SHIPPED ✅ (05 Jun 2026, tag 897a1ed)

### ✅ Dongle-busy retry
Modbus exception code 0x43 = dongle handling another request.
`_hr_write` now detects exception response (inner_func 0x86, code 0x43),
retries up to 6× with 2s delay. Timeouts and echo mismatches still fail immediately.

### ✅ Control confirmation dialog
Every inverter control button (14 total) now shows a confirmation modal before
sending. Prevents accidental taps on touchscreens. Tap outside or Cancel dismisses.

### ✅ Configurable chart colours
7 colour pickers in Settings (Solar, Home, Grid In/Out, Bat Chg/Dis, SOC line).
Stored in config.ini `[colours]`. Served unauthenticated via `/api/colours` so
colours apply at app startup before any chart draws. sdot swatches sync too.

### ✅ SOC clamp + hourly query fix
IR59 (SOC%) now clamped to max(0,min(100,...)) at decode time — prevents garbage
uint16 values (from malformed frames) reaching the DB or live display.
Hourly AVG query now filters `CASE WHEN soc BETWEEN 0 AND 100` to sanitise
any corrupt values already in existing databases.
Root cause of the "SOC off the top of chart" anomaly seen in v1.6 screenshot.

### ✅ home_w zero-blip smoothing improvement
Pi DB had 278 zero home_w runs in 3 days; longest was 100s (10 polls).
Old threshold: 3 polls (30s) — too short, zeros punching through to display.
Fix: home_w debounce raised 3 → 12 polls (120s). solar_w/battery_w stay at 3
(genuine zero periods last hours, so they still reach DB correctly).
`_smooth()` now runs BEFORE `_log_snapshot()` — DB also stores last-good value
rather than raw zero. history kWh totals use counter deltas, unaffected.

### ✅ Adaptive poke (reduce Gen2 75s drops)
Previously both Gen2 (0x32) and Gen3 (0x11) poke frames were sent every 10s.
Gen2 dongle occasionally reacted to the unexpected 0x11 frame, contributing to
75s drops (~1-2/hour).
Fix: `_send_pokes()` checks `_inverter_slave`. Once known, only sends that slave's
frame. `_POKE_BY_SLAVE` dict maps slave → frame. Discovery mode still sends both.
Mirrors GivTCP behaviour. 75s drops significantly reduced (now ~1/session from
cloud service stealing dongle — external, can't prevent).

### ✅ Battery cell detail popup
On-demand BMS read from LV battery modules (slaves 0x32–0x37).
Uses Gen2/LV CRC convention: `_bms_crc16()` = MSB-first over func+base+count.
Reads IR(60,60): 16 cell voltages, 4 group temps, BMS PCB temp, SOC%, cycles,
BMS firmware, health/warning flags.

`/api/battery` endpoint: no auth, opens fresh socket (listen loop recovers ~5s).
Blocked with error for AIO/gateway profiles.

Frontend: tap battery bar → popup modal showing:
- Health badge (green/amber/red from warning register)
- SOC%, cycle count, BMS firmware version
- 4×4 cell voltage grid, colour-coded by deviation from mean
  (≥−30mV = green, −30 to −60mV = amber, <−60mV = red)
- Total / Min / Max / Mean (in V, 3dp) + Spread (in mV)
- Group temperatures + BMS PCB temp + min/max range
- Module tabs if NUM_BATTERIES > 1

Confirmed working on Andi's Gen2 Pi (16 cells, all ~3.348V, 300 cycles, spread 2mV).
Gen3 untested — ask Brendon to tap battery bar and report.

---

## v1.8 — REMAINING CANDIDATES

### ✅ Gateway AIO 10-second polling  — RESOLVED 05 Jun 2026, shipped in v1.8
David's `--aio` capture (capture_20260605_093820) proved active IR(1600,60)@10s +
IR(1780,60)@60s returns FRESH data every poke (battery taper + solar decline tracked
live). The old "active polling = stale" assumption was wrong. Re-enabled in commit
ae9031b, gated on gateway_aio profile; detection now triggers on the gateway's
all-zero base=0 page so polling starts in ~10s, not ~5min. Decoder verified against
the real capture (895/303/593chg/1imp/99%). Gen2/Gen3 poke routing unchanged.
**Was the last blocker for v1.8 — now pre-released. Awaiting David's confirmation
that his dashboard now refreshes every 10s.**

### ✅ Capacity-weighted SOC fallback — SHIPPED in v2.4 (10 Jun 2026, commit 13b3748)
If IR59 reads 0 with no good prior value, falls back to
`remaining_cap / design_cap × 100` (IR88-89 / IR86-87), capacity-weighted across
modules, rate-limited to one BMS read per 5 min, single-phase LV profiles only.

### 🔲 Network scan / auto-discovery
**Goal:** Nicer onboarding — find the inverter IP automatically.
"Scan Network" button that probes the subnet for GivEnergy adapters on 8899.
Auto-fill the IP field; serial already auto-detected.

### ✅ Unit test suite — SHIPPED in v2.4 (10 Jun 2026, commits 8b16de1 + 13b3748)
`tests/test_core.py` — 45 tests over `_classify_model`, `_crc16`, `_bms_crc16`,
`_smooth` (incl. SOC spike/seeding + BMS fallback), slot register maps, HHMM/BCD
codecs, ConfigParser inline comments. Run alongside the 24 scheduler tests.

### ✅ ACBC Scheduler — SHIPPED in v1.9 (06 Jun 2026, commit 70a7c6f)
**Slot-free design** (final architecture — all earlier slot-based iterations superseded).
No inverter slot registers touched. Issues the same register writes as the manual controls:
`ENABLE_CHARGE`, `ENABLE_DISCHARGE`, `BATTERY_POWER_MODE`, `CHARGE_TARGET_SOC`,
`BATTERY_CHARGE_LIMIT`, `BATTERY_DISCHARGE_LIMIT`, `BATTERY_SOC_RESERVE`.
Compatible with Octopus Intelligent Flux and other cloud integrations that lock slots.
UAT passed on Gen2 Pi, 06 Jun 2026. Scheduler icon visible in header. Master defaults OFF.
Full design documented in PROJECT_MEMORY.md §17.

**Post-v1.9 fix (bug #9, commit e6f2769, 06 Jun 2026):**
Power limit display in rule editor used raw register 0–50 labelled "% power" — confusing
(max showed as 50%). Fixed to use watts/% display matching the manual Control screen.
Deployed to Pi. Will be noted in v2.0 release notes.

---

---

## v2.0 — SHIPPED ✅ (08 Jun 2026, tag v2.0, commit 8460c13)

Headline: the app-held scheduler now actually forces grid charge/export on
standalone installs (bugs #14 + #15), AC-coupled inverters detected correctly,
and a batch of fixes. GitHub release live with all 3 assets (Win exe, Linux zip,
Mac zip). Pi redeployed and confirmed on v2.0. Website uploaded by Andi ✅.

> **Scheduler behaviour change:** v1.9's "no slot writes / works alongside cloud"
> design was proven impossible — the firmware needs an active slot. v2.0 writes
> slots and requires exclusive inverter control. v1.9 website note corrected.

| Commit | What |
|---|---|
| 9bc0d89 | **Bug #15:** scheduler writes charge slot (HR 94/95) / discharge slot (HR 56/57) for each rule; clears on hold/baseline; 00:00→00:01 clamp |
| 42e2e8f | Removed SCHEDULER_SKIP_SLOT_WRITES — scheduler requires exclusive control |
| 4c8c609 | `single_phase_ac_coupled` profile — GIV-AC3.0 shows 1 charge + 2 discharge slots |
| 515da38 | **Issue #16:** connecting lines redraw immediately on tab-return |
| 54e8e86 | Documented Gen2 HR 31/32 hardware limitation (bug #8, closed) |
| ecfbf88 | **Bug #14:** ENABLE_CHARGE_TARGET (HR 20) not written → no grid charge (GitHub issue #1) |
| e6f2769 | Bug #9: scheduler power limit display — raw 0–50 register shown as "%" fixed to watts/% |
| de10789 | Bug #10: SOC spike filter — corrupt IR59 single-poll jumps >5% suppressed in `_smooth()` |
| 8ac769a | Bug #2: setup.sh awk "runaway string constant" — `\"\"` → `""` (reported by Dave Holland) |
| 0d88410 | Status age counter ticks smoothly every second instead of jumping on each data poll |
| post-v1.9 | Particle animation pauses when tab hidden — stops competing with YouTube/video GPU |
| 0d8b33b / 8bbfeb8 / 94b77a3 / 30644fb | Control page refresh button + retry-on-slow-detect + 5s cooldown + tooltip |
| 8460c13 | Bump to v2.0 (APP_VERSION, footer, installer.iss, README) |

---

## v2.1 — SHIPPED ✅ (08 Jun 2026, tag v2.1)

Headline: Quick Actions on the home screen, Met Office postcode lookup, website
user guide, and a raft of UI polish. GitHub release live with 3 assets.
Pi redeployed and confirmed on v2.1. Website uploaded by Andi ✅.

### ✅ Quick Actions (new feature)
- **1hr Quick Charge** and **1hr Quick Discharge** buttons on the home screen.
  One tap forces grid charging or battery export for 60 minutes — no scheduler
  rule needed, countdown shown on button, cancel mid-run supported.
- Quick Action bar hides instantly when navigating to any other screen
  (same pattern as weather badge — handled in `showView()`).
- Buttons pre-render greyed on home-screen entry if inverter profile not yet
  detected (`_qaEnabled` flag + `liveData` cache); no sudden pop-in.
- Buttons disabled while a Scheduler rule is actively running.
- Confirmation dialog before anything is sent to the inverter.
- Enabled via App Settings → Quick Actions (toggle + power % + SOC target).
- Supported profiles: `single_phase_ac_coupled`, `single_phase_2slot`,
  `single_phase_extended`.

### ✅ Met Office postcode lookup
- New **Lookup** button in Weather settings — enter a UK postcode, click, done.
  Dashboard calls the Met Office API and fills the nearest station geohash
  automatically. No more manual geohash hunting.
- Weather badge now appears within ~15–20s of saving settings — no restart needed.

### ✅ Website overhaul
- **Shared nav + footer** via `website/js/components.js` — edit once per release
  instead of updating 8+ HTML pages. Active nav state auto-detected from pathname.
- **`user-guide.html`** — full illustrated user guide covering all 10 sections,
  print-to-PDF via browser, sticky sidebar TOC, all screenshots provided.
- **Weather setup page** added to nav and footer (was orphaned).
- **About the developer** section on homepage and GitHub README (`docs/andi.png`).
- `RELEASE_PROCESS.md` updated: footer/nav bump = edit `components.js` only.

### ✅ Bug fixes & polish
- Battery status line reverted to live charge/discharge rate (had drifted to
  showing charge limit %).
- GitHub issue #15 closed (Met Office setup — resolved by postcode lookup feature
  and new `met-office-setup.html` guide page).

---

## v2.2 — SHIPPED ✅ (09 Jun 2026, tag v2.2)

Real-time cost estimation shipped as planned below (tariff config, cost tiles,
history cost toggle, Octopus Agile/IFlux/Cosy fetch) plus the inverter clock-sync
button. The model-audit code changes shipped in commit 124843f.

### Original plan (kept for reference)

Headline: **Real-time cost estimation** — show what your energy is costing and
earning in actual money, using configurable tariff rates.

This is staged because it touches backend config, the API, the home screen UI,
and eventually the history charts. Do each stage separately and UAT before moving on.

### Stage 1 — Tariff configuration (backend + settings UI)
- New `[tariff]` section in `config.ini`:
  ```ini
  import_rate_p  = 24.5   ; pence per kWh imported from grid
  export_rate_p  = 15.0   ; pence per kWh exported to grid
  standing_charge_p = 53  ; pence per day (optional, for full cost view)
  currency_symbol = £
  ```
- New **Tariff** sub-section in App Settings UI — same pattern as existing sections.
- `/api/data` response gains `import_rate_p`, `export_rate_p`, `currency_symbol`
  so the frontend can compute costs from the existing kWh totals without a new endpoint.

### Stage 2 — Home screen cost tiles
- Enhance or replace the existing Today's Totals tiles to show cost alongside kWh:
  - **Imported:** X kWh · £Y.YY
  - **Exported:** X kWh · £Y.YY (earned)
  - **Net today:** £Y.YY saved / £Y.YY cost
- Calculation: `import_cost = e_grid_in_daily × import_rate_p / 100`
  `export_income = e_grid_out_daily × export_rate_p / 100`
- Works entirely in the frontend — no new API endpoint needed if rates are in `/api/data`.

### Stage 3 — History charts cost overlay
- Add optional cost/income bars (or a toggle) to the daily history chart.
- New `/api/history` response fields: `import_cost`, `export_income`, `net_cost` per period.
- Probably a separate "Costs" tab alongside existing energy tabs.

### Stage 4 — Future (v2.3+, not in scope for v2.2)
- Peak / off-peak time windows with different import rates.
- Octopus Agile live price feed.
- "Savings vs grid-only" counterfactual estimate.

### What we already have (no schema change needed for Stage 1 + 2)
- `e_grid_in_daily` and `e_grid_out_daily` already in `/api/data` response.
- History table already stores per-day `e_grid_in` and `e_grid_out` kWh.
- All the kWh data is there — it's config + frontend work for Stages 1 and 2.

### Inverter time sync button
- GivEnergy inverters hold their own RTC clock in holding registers (typically HR 35–40:
  year, month, day, hour, minute, second). Clock drift is common — especially after a
  power cut — and a drifted clock breaks scheduler rules and history timestamps.
- **Button in Inverter Settings screen** — "Sync time with server" — writes the server's
  current local time to the six RTC registers in one pass using `_hr_write()`.
- Backend: new `/api/control` action `sync_time` — reads `datetime.now()`, writes
  HR 35=year, HR 36=month, HR 37=day, HR 38=hour, HR 39=minute, HR 40=second.
- Frontend: button with confirmation dialog ("Set inverter clock to HH:MM:SS DD/MM/YYYY?"),
  success/error toast. No password re-prompt needed if already unlocked.
- **⚠️ Verify exact register numbers** against `givenergy-local-modbus.json` spec before
  implementing — HR 35–40 is the commonly cited mapping but must be confirmed.
- Low implementation cost; high practical value (silent clock drift is hard to diagnose).

### Also in v2.2 scope — model audit (deferred from v2.1)
The GivEnergy model detection audit was the original v2.1 primary task but was
deferred while Quick Actions and website work took priority. Still needed:
- Correct official names for all DTC prefix mappings (`_DTC_*_MODEL_NAME`)
- Resolve what DTC `0x83` actually is (currently labelled "Hybrid Gen4" — wrong)
- See `PROMPT-model-detection-audit.md` for the full brief
- This is a research + code task; can run alongside cost estimation or separately

---

## v2.3 — SHIPPED ✅ (10 Jun 2026, tag v2.3)

Bug-fix release:
- **#23** Gateway AIO SOC stuck at 0% — spike filter seeded from uninitialised value
- **#24** Startup crash on inline `;` comments in config.ini (5 ConfigParser fixes)
- **#21 (partial)** AIO HR 1100 slot-read crash — retry ×3 + graceful fallback
- **#22** Quick-action revert clobbering user settings — snapshot at action start
- **#19** Sync Inverter Clock button moved above Activity Log

---

## v2.4 — SHIPPED ✅ (11 Jun 2026, tag v2.4, commit 0d06f08)

Bug-fix release: AIO slot detection, quick-action reliability, canvas redraw fix,
dynamic footer version, sim harness + 131/131 regression suite.

- **Issue #21** — detect All in One as single_phase_extended; charge slot 2 uses
  HR 243/244 (57cb736)
- **Issue #22** — quick-action state persisted to disk; startup recovery; resilient
  per-write revert; free-slot selection; manual cancel; every revert logged (93aaa6e)
- **Canvas redraw fix** — requestAnimationFrame defer in showView(); fixes animation
  triangle drift on Chrome mobile (93aaa6e)
- **Dynamic footer version** — driven by d.app_version from /api/data; no hardcode (93aaa6e)
- **Sim harness + regression suite** — 131/131 pass (71 unit, 15 detection, 24 control,
  21 live); report at docs/regression-report-v2.4.md; tools in tools/ (a0370e5)
- **Issue #2 fix** — clear RuntimeError when poll mode lacks the modbus library (675725e)
- **Unit test suite** — tests/test_core.py, 45 tests (8b16de1, b05dba8, 13b3748)
- **Capacity-weighted SOC fallback** — IR59 stuck at 0 falls back to BMS ratio (13b3748)
- **gen3-capture --slots mode** — HR sweep; shipped on gen3-capture-1.0 release (0990a59)

---

## v2.5 -- SHIPPED ✅ (13 Jun 2026, tag v2.5)

Reliability release. GitHub release live with all 3 assets. Pi redeployed on v2.5 ✅.

- **Write retry on first-write timeout** -- reg 56 / reg 94 (slot 1 start) timed out
  consistently as first write in a sequence; `_hr_write` now retries once after 1.5s.
  Targets issue #21 (AIO slot saves) and issue #22 (revert pattern). Awaiting
  tester confirmation on both.
- **Quick-action snapshot abort** -- action aborts cleanly if pre-action HR read fails
  rather than silently restoring wrong defaults (issue #22 fix, commit ad42124).
- **Revert read-back verification** -- each revert write individually confirmed; Activity
  Log now accurately says "reverted and verified" / "writes ok but N did not hold" /
  "revert partial" (commit ad42124). Awaiting Paroparo1954 re-test.
- **Per-slot Set button + dirty tracking** -- save any slot without re-saving all;
  edits survive other control actions (commit 30206b4).
- **Power limit sliders 0-100%** -- was showing raw 0-50 register value; now
  register x2 display, display/2 write (issue #21 follow-on).
- **Geohash validation** -- rejects non-6-char codes client + server side (issue #25).
- **75s watchdog regression fixed** -- EOF detection on recv(), async weather fetch
  thread, watchdog interval 75s -> 30s.
- **Backup import size limits** -- 10 MB raw, 100 MB decompressed (issue #28 closed).
- **VERSION file** -- single source of truth; installer.iss + Python both read from it
  (issue #29 closed).
- **requirements.txt** -- runtime deps only: flask + givenergy-modbus (issue #30 partial).
- **Issue #17** -- model names aligned to GivEnergy portal naming; String Inverter Gen 3
  mapped to unknown profile (issue #17 closed).

---

## v2.6 -- SHIPPED ✅ (14 Jun 2026, tag v2.6)

Solar Forecast panel, auth toggle, Quick Action and AIO echo fixes. GitHub release live with all 3 assets.

### ✅ Solar Forecast panel
Powered by Forecast.Solar free tier. Shows today and tomorrow's estimated solar generation
in kWh, a next-2-hours highlight, and an hourly bar chart across both days.
Set up under App Settings -> Solar Forecast. Single-array only in this release --
multi-array support and predictive charge target parked for later (issue #37).

### ✅ Auth toggle
New "Require password for controls" setting. When turned off, Quick Actions, Control,
Scheduler and Activity Log are accessible without a password. Settings, Tariff and Backup
remain password-protected regardless.

### ✅ Quick Action and Control fixes
- Quick Charge / Export confirmation replaced with the dashboard's own in-page modal --
  browser confirm() could be permanently suppressed, silently blocking the action.
- Quick Action and Control buttons now prompt for the admin password themselves when the
  session is locked.
- AIO inverter: stale echo frames no longer cause false "echo mismatch" errors on batch
  slot saves.
- Met Office fog icon (codes 5/6) replaced with a compatible glyph (was a white box on Windows).

### Closed with v2.6
- **Issue #35** -- Upgrade Notification following v2.4 upgrade: CLOSED 13 Jun 2026.
  Self-resolved as predicted -- browser cache cleared on v2.5 install.

---

## v2.8 -- SHIPPED ✅ (19 Jun 2026, tag v2.8)

### ✅ Settings page reorganised into 4 sub-panels (issue #44)
Display, Integrations, Admin, Inverter -- each with its own Save button. UAT approved on Pi.

### ✅ Slot visibility fix + expanded to all inverters (issue #45)
Bug: `00:00` string was truthy so hide logic never fired. Fixed by treating `00:00` as empty.
+/- control now renders on any inverter with >1 slot (was AIO-only).

### ✅ Scheduler drift detection (issue #47)
`_sched_drift_loop()` thread reads back scheduler-controlled registers every 5 minutes,
grouped into minimal contiguous reads. Drift clears `_sched_applied_sig` and forces re-apply.
Drift events logged at WARNING and shown in Activity Log.

### ✅ Control page cache fix (issue #39)
`GET /api/control` now returns `Cache-Control: no-store`. Stale cached response after
external inverter change no longer possible.

### ✅ JSON validation hardening (issue #31)
All POST routes: `get_json(force=True)` → `get_json(silent=True)` for consistent
handling of malformed/missing request bodies.

### ✅ Content-Security-Policy header (issue #32, partial)
CSP header added to all responses. Full `textContent` migration deferred.

### ✅ DB schema versioning (issue #33)
`PRAGMA user_version` tracking added. Schema stamped v1 on first startup.
Future changes use `if _db_version < N:` guards.

### ✅ Weather icons replaced with Meteocons
Meteocons fill-style SVGs (23 icons, locally hosted). Full day/night variants for all
30 Met Office DataPoint weather codes.

### Issue status at v2.8 close

| Issue | Outcome |
|---|---|
| #44 -- Settings 4 sub-panels | ✅ Shipped v2.8 |
| #45 -- Slot visibility bug | ✅ Fixed v2.8 |
| #47 -- Scheduler drift detection | ✅ Shipped v2.8 |
| #39 -- Control page refresh | ✅ Fixed v2.8 |
| #31 -- JSON validation | ✅ Shipped v2.8 |
| #32 -- XSS / CSP | ✅ Partially shipped v2.8 (CSP header done) |
| #33 -- DB schema versioning | ✅ Shipped v2.8 |
| #37 -- Predictive charge | Parked -- v2.9 headline feature |
| #48 -- Export cutoff SOC workaround | Parked -- research needed (register map) |

---

## v2.7 -- SHIPPED ✅ (15 Jun 2026, tag v2.7)

### ✅ Persistent log file with rotation (issue #38, 14 Jun 2026)
`TimedRotatingFileHandler` writes to `dashboard.log`; daily rotation; retention
configurable 1-30 days. Log level (warning / info / debug) and retention both
editable in Settings -> Diagnostics. Download log button in same section.
Config keys added to `[logging]` section in config.ini. Deployed to Pi and confirmed.

### ✅ Slot visibility control (14 Jun 2026)
"Showing N of 10 slots" +/- control above the charge and discharge slot grids; only
rendered for single_phase_extended profile. Persisted in localStorage. Slots that have
values are always visible regardless of the counter. Suggested by Gaz (issue #21).

### ✅ Gen1 listen-socket snapshot timeout (15 Jun 2026)
Gen1 dongle only responds to on-socket HR reads up to ~HR 43. Fix: poll fallback via
fresh `_hr_read()` connection when on-socket read fails. Shipped in v2.7.

### ✅ AIO HR 313/314 charge/discharge power limits (issue #21, confirmed 15 Jun 2026)
HR 313/314 confirmed as the correct power limit registers for single_phase_extended.
AIO tester confirmed fix working in v2.7. Issue #21 closed 15 Jun 2026.

### Issue status at v2.7 close

| Issue | Outcome |
|---|---|
| #21 -- AIO HR 313/314 power limits | ✅ Confirmed by AIO tester, closed 15 Jun 2026 |
| #27 -- scrypt password hashing | ✅ SHIPPED (commit 0dc8270) |
| #37 -- Predictive overnight charge target | Parked -- v2.8 candidate |
| #26 -- Pi appliance image | Parked v2.8+ |
| #31 -- JSON input validation | Parked -- v2.8 candidate |
| #32 -- XSS hygiene / CSP | Parked -- v2.8 candidate |
| #33 -- DB schema migrations | Parked -- v2.8 candidate |
| #34 -- Defender submission process | Parked (no code-signing, decided 10 Jun 2026) |

---

## Future / parked

GitHub is the source of truth for all tracked bugs and enhancements.
The only exception is pre-release UAT items found by Andi before a version ships.
Do not maintain enhancement descriptions here -- open a GitHub issue instead.

### Shipped in v3.0 (7 Jul 2026)

- **#53 -- Official Docker image** (headline) -- multi-arch (amd64/arm64/arm-v7) image published to `ghcr.io/acbcsoftware/givenergy-dashboard`. `DATA_DIR` refactor separates writable state onto a `/data` volume; `/healthz` liveness endpoint; gosu entrypoint; publish workflow on `v*` tag. Full UAT on spare Pi 4 incl live inverter. Website `install-docker.html` added.
- **#55 -- Octopus tariff base-rate fix** -- base is now the dominant flat rate (was the cheapest band); every cheaper AND pricier band becomes its own TOU window, with multi-day dedupe. Fixes standard-rate hours costed at off-peak on Flux/Cosy/Go/Intelligent Flux. Users on a fetched Octopus tariff must re-fetch. Open until Steve confirms on hardware.
- **Integration logging** -- Debug/Info/Warning logging across all outbound integrations (tariff, weather, solar forecast, GitHub update check, postcode lookup); secrets never logged.
- Tests added: `tests/test_tariff.py` (11), `tests/test_predictive.py` (14).

### Shipped in v2.9 (4 Jul 2026)

- **#37 -- Predictive overnight charge target** -- opt-in per charge schedule; target SOC computed from tomorrow's solar forecast minus historical morning demand.
- **#51 -- Year in Review** -- annual highlights page with best/worst days, average daily profile, and a carbon-offset badge (CO2 avoided / trees / car miles).
- Battery BMS splice guard (learned from givenergy-modbus #256) and live charge-status indicator.
- Control-page initial-read retry with backoff and logging (#49 mitigation).

### Open GitHub issues (as of v2.9, 4 Jul 2026)

| # | Title | Notes |
|---|-------|-------|
| [#26](https://github.com/ACBCSoftware/acbc-givenergy-dashboard/issues/26) | Flash-and-go Pi appliance image | Low -- larger effort |
| [#34](https://github.com/ACBCSoftware/acbc-givenergy-dashboard/issues/34) | Windows Defender / SmartScreen submission | Parked -- no code-signing (decided 10 Jun 2026) |
| [#40](https://github.com/ACBCSoftware/acbc-givenergy-dashboard/issues/40) | Include register number in control activity log error messages | Low effort polish |
| [#41](https://github.com/ACBCSoftware/acbc-givenergy-dashboard/issues/41) | Audio cue when slot overlap validation rejects a save | Low priority |
| [#42](https://github.com/ACBCSoftware/acbc-givenergy-dashboard/issues/42) | Per-slot lower SOC limit on discharge slots (single_phase_extended) | AIO tester request |
| [#43](https://github.com/ACBCSoftware/acbc-givenergy-dashboard/issues/43) | Diagnostic tool: register value-search mode | Low priority tool |
| [#46](https://github.com/ACBCSoftware/acbc-givenergy-dashboard/issues/46) | Further considerations (AIO tester suggestions) | Winter mode, tooltips, multi-array |
| [#48](https://github.com/ACBCSoftware/acbc-givenergy-dashboard/issues/48) | Export cutoff SOC workaround | Research needed -- register map |
| [#49](https://github.com/ACBCSoftware/acbc-givenergy-dashboard/issues/49) | Occasional blank Inverter Control screen | Mitigated in v2.9 (retry + logging); watching for recurrence |
| [#52](https://github.com/ACBCSoftware/acbc-givenergy-dashboard/issues/52) | Max charge/discharge rate registers on AIO | AIO uses HR313/314; confirmed behaviour, offered read-only 111/112 display |
| [#53](https://github.com/ACBCSoftware/acbc-givenergy-dashboard/issues/53) | Official Docker image | Enhancement request (2 users) |
| [#54](https://github.com/ACBCSoftware/acbc-givenergy-dashboard/issues/54) | National Grid live data feed ticker | Enhancement request |

### Ideas -- not yet filed as GitHub issues

Raise a GitHub issue when one of these is ready to act on.

- **Installer stop: use PID file instead of port-kill** -- write PID file at startup, verify command line before killing. Low risk but correct long-term.
- **Module split: break up dashboard_server.py** -- 4,600+ lines. Large refactor, not a near-term ticket.
- **Encoding audit** -- verify files are clean UTF-8; add .editorconfig if not already present.
- **Android tablet support** -- dedicated display on an old 8" tablet. Longer term.
- **Ad-hoc charge/discharge with start time + duration** -- period-based forcing like GivTCP's mode. Complements Quick Actions.
- **AIO2 battery cell detail** -- dongle broadcasts BCU/module pages unprompted; passive collection could enable the battery popup with no extra polling.
- **Model detection & naming audit** -- trawl GivEnergy manuals + GivTCP DTC tables; resolve speculative 0x83 mapping. Self-contained prompt: `P:\givenergy\PROMPT-model-detection-audit.md`.
- **Cost / tariff tracking** -- peak/off-peak windows + rates; classify kWh deltas on History page. Counters already in DB.
- **Temperature trend chart** on hourly page -- data accumulating, needs charting.
- **Power-limit calibration helper** -- one-tap %->W scaling validation.
- **Octopus Agile / tariff integration** -- price feed for smarter scheduling.
- **Multi-inverter support** -- a few users have asked.
- **WebSocket push** -- currently polls /api/data; only worth it for sub-second UI updates.
- **Capacity-weighted SOC for Gateway AIO** -- IR1801 can read 0%; fallback would use BMS sub-block if accessible.

---

## Known supported hardware (as of v1.8 in progress)

| Model | Reading | Control | Battery Detail | Notes |
|---|---|---|---|---|
| Gen2 / AC-coupled (GIV-AC3.0) | ✅ 10s | ✅ library-free | ✅ confirmed | Andi's unit; slave 0x11/0x32, DTC 0x3xxx, profile single_phase_ac_coupled — **1 charge + 2 discharge** slots |
| Gen2 hybrid | ✅ 10s | ✅ library-free | ✅ confirmed | DTC 0x2xxx (fw century 8/9); profile single_phase_2slot — 2 charge + 2 discharge |
| Gen3 / HV hybrid | ✅ 10s | ✅ library-free | ⚠️ untested | Slave 0x11; 10 slots; Brendon to test battery tab |
| Gateway AIO | ✅ ~5min | ⚠️ read-only | ❌ unsupported | DTC 0x70xx; 10s polling = v1.8 pending |
| Three-phase / AIO Commercial | ✅ | ⚠️ read-only | ❌ unsupported | DTC 0x4x/0x82; writes pending capture |

---

## Bug log

All bugs are now tracked as GitHub issues:
**https://github.com/ACBCSoftware/acbc-givenergy-dashboard/issues**

Open issues and historical closed issues are the single source of truth.
Do not maintain a parallel bug table here.
