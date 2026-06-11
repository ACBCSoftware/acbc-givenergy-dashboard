That's really helpful, thank you, and the discharge 2 = 44/45 is exactly what we hoped, so that side of the map is nailed.

Your heads-up about the Direct Control app is the important bit, though. You're right to be wary: if that app is reading or writing registers that aren't actually used on the All in One, then the one surprise in your screenshots (charge slot 2 showing up at 243/244 instead of the usual address) could just be the app mislabelling, not your inverter's real layout. So I don't want to bake that number in on the app's say-so. Your instinct to verify it properly is spot on.

And the good news is the test you described is basically already built. It's the same diagnostic tool, just the right mode this time. Here's the plan:

**Step 1: set distinctive test times in the official GivEnergy app** (not the Direct Control app, because we want the real schedule path to write the registers). Give each slot a unique value so there's no mistaking which is which:

| Slot | Start | End |
|---|---|---|
| AC Charge 1 | 01:01 | 01:02 |
| AC Charge 2 | 02:01 | 02:02 |
| DC Discharge 1 | 03:01 | 03:02 |
| DC Discharge 2 | 04:01 | 04:02 |

(The hour is just a label: slot 1 = 1 o'clock, slot 2 = 2 o'clock, and so on, so whatever register holds "02:01" is unambiguously your charge slot 2.)

**Step 2: run the register sweep.** Use the same **gen3-capture-windows-slots.zip** download as before, but this time double-click **run-slots.bat** specifically. That's the bit that got missed last time: the file you ran did a live-data capture (NORMAL mode) rather than the register sweep, which is why nothing jumped out at you in the log. run-slots.bat reads back the actual register ranges and flags anything that looks like a time. It finishes by itself in about a minute.

**Step 3: send the `capture_..._log` file back.**

That dump will show exactly which register each of those test times landed in, with the app's labels taken completely out of the picture, and then we'll have your All in One's slot map nailed down for real, not on trust.

Genuinely appreciate the rigour on this. It's the difference between guessing and knowing.
