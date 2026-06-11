Thanks for testing v2.3 and for the diagnostic files — they told us a lot. Good news on the model/serial now populating correctly.

**What we found in the capture:** you were right to flag the "3 phase" mention while you're on single phase — that turns out to be the actual bug. The dashboard currently treats the All in One 2 as a *three-phase* device, so it looks for schedules in the register range where three-phase inverters keep them (HR 1100+). Your inverter doesn't have those registers at all — hence the timeouts and the empty 00:00:00 slots. The capture also confirmed the rest of your system responds like a normal single-phase inverter (live data, battery modules and all — lovely clean capture by the way).

**What we need now:** one more short run with a new diagnostic mode that sweeps the *single-phase* register ranges to find exactly where your AIO keeps its schedule slots. Once we see them, v2.4 will read the right registers.

**Steps (about a minute, read-only, changes nothing on your system):**

1. Make sure the schedules you set in the GivEnergy app are still in place (the distinctive times you used are perfect — 01:00–01:30, 02:00–02:30, 18:00–19:00, 20:00–21:00). If you cleared them, please set them again — they're what makes the slot registers stand out in the dump.
2. Download **gen3-capture-windows-slots.zip** from: https://github.com/ACBCSoftware/acbc-givenergy-dashboard/releases/tag/gen3-capture-1.0
3. Unzip, open `gen3_config.ini` in Notepad, set your inverter's IP (same one as before), save.
4. Close the GivEnergy phone app and stop anything else talking to the inverter (including the ACBC dashboard if it's running).
5. Double-click **run-slots.bat**. It probes a handful of register ranges and prints anything that looks like an HH:MM time, then finishes by itself.
6. Attach both `capture_...log` and `capture_...bin` files here (zipped, as before).

If the times you set show up in the dump — and we expect they will — the v2.4 release will have your schedules displaying properly, and it'll stop calling your single-phase system "three-phase" while we're at it.

Thanks again for the help — this is exactly the data that gets AIO support right.
