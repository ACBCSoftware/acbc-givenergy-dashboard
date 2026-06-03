ACBC GivEnergy Dashboard — macOS Installation
=============================================

Requirements: macOS 11 (Big Sur) or later, and Python 3.9+.
              If you don't have Python, get it from https://www.python.org/downloads/
              (or run: brew install python)


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INSTALL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Unzip this folder anywhere (e.g. your Desktop).

2. Double-click  setup-mac.command

   • macOS may show: "setup-mac.command can't be opened because it is from
     an unidentified developer." If so:
       → Right-click (or Control-click) setup-mac.command → Open → Open.
     You only need to do this once.

   • If double-click still won't run it, open Terminal and run:
       cd ~/Desktop/givenergy-dashboard-mac    (wherever you unzipped it)
       bash setup-mac.command

3. The installer will:
     • Create a Python environment in
       ~/Library/Application Support/ACBCGivEnergyDashboard
     • Install the required packages
     • Generate the app icons
     • Register a start-on-login service (launchd) so the dashboard runs
       automatically and restarts if it ever stops
     • Open the dashboard in your browser

4. Set your inverter's IP address:
     • Open the ⚡ Settings screen in the dashboard, OR
     • Edit ~/Library/Application Support/ACBCGivEnergyDashboard/config.ini


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
USING IT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Open in any browser:   http://localhost:7890
From your phone:       http://<your-mac-ip>:7890   (same WiFi)

The dashboard runs in the background and starts automatically when you log in.

Start / stop manually (in the install folder):
   start-dashboard.command
   stop-dashboard.command

Default settings password: password  — change it on first use.


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ADD TO YOUR PHONE (optional)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

iPhone (Safari): open http://<your-mac-ip>:7890 → Share → Add to Home Screen.
Android (Chrome): menu → Add to Home Screen.


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
UPGRADING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Download the new mac zip, unzip, and run setup-mac.command again.
Your config.ini (settings) and history.db (logged data) are always preserved.


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
UNINSTALL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

   launchctl unload ~/Library/LaunchAgents/com.acbcsoftware.givenergy.plist
   rm ~/Library/LaunchAgents/com.acbcsoftware.givenergy.plist
   rm -rf "~/Library/Application Support/ACBCGivEnergyDashboard"


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TROUBLESHOOTING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

"python3: command not found"
   → Install Python from https://www.python.org/downloads/ and re-run.

Dashboard doesn't load / "Connecting…"
   → Check your inverter IP in config.ini. Mac and inverter must be on the
     same network. Port 8899 must be reachable.

View the log:
   ~/Library/Application Support/ACBCGivEnergyDashboard/dashboard.log
