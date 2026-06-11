Brilliant — those screenshots cracked it completely, thank you. That "Direct Control" register list is exactly what we needed.

Here's what they tell us: your All in One keeps its charge/discharge schedules in the **standard single-phase registers** — AC Charge 1 at 94/95, DC Discharge 1 at 56/57, and the extended slots from 243 upward — every one matching the times you'd set. In other words your unit behaves like a single-phase hybrid (the same family as a Gen3 HV), **not** a three-phase machine.

That's the whole bug in a nutshell: the dashboard had your All in One pegged as three-phase, so it was looking for the schedule slots in the three-phase register block (1100+) — registers your inverter doesn't have, which is exactly why you saw the timeouts and 00:00:00. v2.4 will detect it as single-phase and read the right registers, and your schedules will show properly.

(Also — thanks for the Gen 1 correction. You're right, and it's flagged something we'd got muddled in our model naming, so that's getting fixed at the same time. And no offence taken on the GE dig — none of us are them!)

One last thing would nail it. Your screenshots showed **AC Charge 2** (register 243/244) and **DC Discharge 1** and **3**, but not **DC Discharge 2**. Could you scroll to **DC Discharge 2 Start Time** in that same Direct Control list and let me know its register number and value? It should read 20:00 (since that's what you set it to). That single number tells us whether discharge slot 2 sits at the newer spec address or an older one — it's the last piece for getting all ten slots correct on the All in One.

Don't worry about re-running the diagnostic tool — your screenshots were genuinely more useful than the tool would have been, so that side's done.

And yes please, very happy for you to rally the SpeakEV All in One folks — a couple more captures from different All in One vintages would help us confirm the layout holds right across the range. The more the merrier.

Thanks again — properly useful detective work.
