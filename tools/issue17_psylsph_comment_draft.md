## All in One reclassified to single-phase extended (with #21) + cross-referenced

The headline correction from this audit is now confirmed on hardware and implemented.

**The All in One is single-phase, 10-slot extended, not three-phase.** Our AIO tester ran a sentinel-value register sweep (unique times across all 10 charge + 10 discharge slots, written via a trusted third-party app, read straight back). Result: a complete, unambiguous map that matches the `single_phase_extended` layout. The three-phase slot block (HR 1100+) does not exist on the unit (both reads timed out). Two corrections fell out of it:

- **DTC.** A direct HR[0] read returned `0x8001` ("All in One", Gen 1), not the `0x8201` we had assumed in earlier notes. So the real culprit was the coarse `"8" => three_phase_aio` rule catching the whole `0x8xxx` family, not a single DTC.
- **Charge slot 2.** It sits at HR 243/244 (the contiguous extended block), not the library's HR 31/32. Every other slot (the other 19 start/end pairs and all 20 SOC targets) already matched our extended map. Discharge slot 2 stays at the legacy HR 44/45, which we already had right.

**Cross-reference.** psylsph's `home-energy-manager` and `givenergy-simulator` both class All in One as 10-slot extended, which corroborates the profile. Note they both use HR 31/32 for charge slot 2 (same library convention we did), so the 243/244 finding appears to be new community data. The simulator's DTC table also confirms the `0x80xx`/`0x81xx` AIO family.

**Implemented** (commit on `main`): the whole `0x8xxx` family now maps to `single_phase_extended`; charge slot 2 uses HR 243/244 on the extended profile via a profile-aware helper, while 2-slot/gateway keep HR 31/32. This enables control and the in-app scheduler on the All in One. Detection tests added for the family. Tracked end-to-end in #21.

**Still open on this issue:** finalising the friendly names for the `0x80`/`0x81`/`0x82` AIO family (the simulator labels `0x81xx` "AIO 8/10kW" where we say "Hybrid HV Gen3" — same profile, different name), and the unverified `0x41xx` (AIO Commercial) / EMS rows that have no tester. These are naming-only, lower priority than the profile fix that's now done.
