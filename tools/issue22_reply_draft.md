Thanks, that Remote Control History told me a lot.

First, do not worry about the version number at the bottom of the dashboard. I have just found it can show an old number even when the actual program underneath is up to date (my own test Pi does exactly that), so that footer is not a reliable guide and I will fix it so it always shows the true version. The thing that matters is that you are no longer being offered the update, which tells me the core of your install is on v2.3, which is the part involved in this.

What would pin down the cause is one thing. In Inverter Settings there is an Activity Log. Could you copy out the entries from around the time this morning's export finished, roughly 11:25 to 11:45? I am looking for any line that mentions "Quick action" or "revert". That single entry tells me exactly what the auto-revert did on your system.

If you would like to see the exact version for yourself, open the same address you use for the dashboard with /api/data on the end, and look for "app_version" in the text. Entirely optional.

Thanks again, this is genuinely helpful detective work.
