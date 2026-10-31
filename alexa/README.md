# Alexa / Echo Show

Ask an Echo about the battery, solar, the house and the grid. On an Echo Show the
dashboard also opens full-screen, and you can change the view by voice. It is a
**private** skill: you never publish it, and it only works on Echos signed in to
your Amazon account.

```
"Alexa, open the energy portal"         → summary, and the dashboard on screen
"Alexa, ask the energy portal what's the battery at"
  …while it's open: "Alexa, show history" / "Alexa, how much solar today"
"Alexa, stop"                             → closes it
```

## What you need

- The dashboard reachable over **HTTPS** from the internet. The Cloudflare
  Tunnel in the main README does this, and no new hostname is needed: Alexa
  calls `https://<your-domain>/alexa` and the Echo opens `https://<your-domain>/echo`.
- The `cryptography` Python package. Every request is checked against Amazon's
  signature, and anything that fails is refused.
  `venv/bin/pip install cryptography`
- A free Amazon developer account at [developer.amazon.com](https://developer.amazon.com),
  signed in with the **same Amazon account as your Echo**.

## 1. Create the skill

1. Go to [developer.amazon.com/alexa/console/ask](https://developer.amazon.com/alexa/console/ask)
   and choose **Create Skill**.
2. Name: anything, e.g. *The Energy Portal*. Locale: **English (UK)**.
3. Experience: **Other**. Model: **Custom**. Hosting: **Provision your own**.
4. Template: **Start from Scratch**, then **Create**.

## 2. Load the interaction model

1. In the skill, go to **Build → Interaction Model → JSON Editor**.
2. Paste the contents of `interaction-model.en-GB.json`, then **Save** and **Build skill**.
   If the skill has more than one language (e.g. English (US) as well), paste it
   into each one: every language has its own model.

The JSON is generated: edit `build_model.py` and run `python alexa/build_model.py`.
It refuses to write a model with a sample used by two intents or a `{slot}` the
intent doesn't declare, which the console only reports after a slow build.
3. To use a different wake phrase, change `invocationName` before building, and
   set the same words on the **Distribution** tab (the Echo goes by those), in
   every language the skill has.
   It must be at least two words, in lower case.

## 3. Turn on the screen interface

**Build → Interfaces →** turn on **Alexa Web API for Games**, then **Save**.
That interface is what lets the skill open the dashboard full-screen on the Show.

## 4. Point it at your dashboard

1. **Build → Endpoint → HTTPS**.
2. Default Region: `https://<your-domain>/alexa`
3. SSL certificate type: **My development endpoint is a sub-domain of a domain
   that has a wildcard certificate from a certificate authority**. This is the
   right choice for a Cloudflare-fronted domain.
4. **Save**. Copy the **Skill ID** shown at the top of the Endpoint page.

## 5. Configure the dashboard

In `config.ini`:

```ini
[alexa]
skill_id      = amzn1.ask.skill.xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
allow_control = no
```

Restart the dashboard. On a Pi: `sudo systemctl restart givenergy`.

## 6. Enable testing

In the console, open the **Test** tab and set *Skill testing is enabled in* to
**Development**. The skill is now live on every Echo on your account. Type
`open the energy portal` in the test simulator to check it before trying it on a device.

## What you can ask

Any of these after *"Alexa, ask the energy portal …"*, or on their own while it's open.
A period can be today, yesterday, this or last week, month or year ("month to
date", "the last seven days", "last month's" and so on also work). Leave it out
and you get today (this year for savings), and Alexa says so. Then *"what about
last month?"* asks the same question again for another period.

| Topic | Examples |
|---|---|
| Live | how are we doing · what's the battery at · how much solar · what's the house using · are we exporting |
| Cost (money spent) | what did this month cost · what's the damage this week · how much have we paid this year · what's the bill |
| Earnings, savings | how much did we earn from exporting last month · how much has solar saved us this year · give me the year in review · what's our estimated annual cost |
| Solar | how much of today's solar did we use ourselves · how self sufficient are we this week · how does this week compare to last week · what was our best solar day this month · when did we use the most power today |
| Tariff (price per unit) | what's the electricity rate right now · is power cheap at the moment · what's the export rate · what's the standing charge · what tariff are we on |
| Cheap times | when is the cheapest time to charge tonight · when is power cheapest today · is now a good time to run the washing machine |
| Forecast, weather | what's the solar forecast for tomorrow · is tomorrow better than today for solar · what's the temperature outside |
| Battery | how healthy is the battery · what's the battery temperature · how many cycles has the battery done · how much usable capacity do we have left · how long will the battery last · when will the battery be full · what's the battery set to charge to tonight |
| Inverter, system | what mode is the inverter in · is the inverter online · is the backup circuit on · what's the grid voltage · what's the scheduler doing tonight · is everything working · when was the last backup · did we lose connection to the inverter today |
| Screen | show history · show the forecast · show the battery · show year in review · show the live view |

Cost reads out import, export credit, standing charge and the net figure, then
offers the kWh ("Want the energy figures?" - say yes). Each cost request is
logged with what Alexa heard and the dates it used, as
`Alexa: CostIntent period heard='month to date' -> this month (2026-10-01 to 2026-10-31)`,
so a misheard period shows up in the log. Weeks start on Monday, and all dates
are UK dates, clock changes included.

## Voice control

With `allow_control = yes`:

- *"switch to storage mode"* / *"switch to eco mode"*
- *"turn on export"* / *"switch to demand only"*
- *"set the battery reserve to 20 percent"*
- *"hold the battery charge until six"* (needs the scheduler on; not kept over a restart)
- *"turn the scheduler on"* / *"off"*
- *"sync the inverter clock"*
- *"charge the battery"*, *"quick discharge"*, *"stop charging"* - the dashboard's
  one-hour **Quick Actions**, which must also be enabled in Settings

Every one of them asks first ("Switch the inverter to storage mode. Are you
sure?") and only acts on a "yes" within two minutes, then says what it did.

Controls also follow the dashboard's **require password for controls** setting.
The Echo can't type the password, so while that's on (the default) controls are
app-only and Alexa says so. Anyone who can talk to the Echo can use them once
both are allowed, which is why voice control is off by default.

## Things to know

- **The screen times out.** The Echo closes the dashboard after 30 minutes
  without interaction. That is the longest Amazon allows for this kind of
  display. Say "open the energy portal" again to bring it back.
- **Cloudflare blocks Alexa:** if the skill says *"There was a problem with the
  requested skill's response"* and the dashboard log shows nothing, check
  Cloudflare → Security → Events. *Bot Fight Mode* and some WAF rules block
  Amazon's servers. A **Cloudflare Access** login in front of the domain needs a
  Bypass policy for the `/alexa` and `/echo` paths.
- **Rejected requests** are logged as `Alexa: rejected request - <reason>`.
- On an Echo **without a screen**, everything except the views works the same,
  with spoken answers only.
- Testing a new question: in the console's **Test** tab, the JSON Input panel
  shows which intent and slot value Alexa picked. The dashboard log line for
  each request shows the same (`Alexa: IntentRequest CostIntent {'period': ...}`).
