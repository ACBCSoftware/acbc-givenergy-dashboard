"""Builds interaction-model.en-GB.json, the model pasted into the Alexa
developer console (Build > Interaction Model > JSON Editor).

Edit the intents and slot types here rather than in the JSON: the checks at
the bottom catch the mistakes the console only reports after a slow build - a
sample used by two intents, a {slot} the intent doesn't declare, digits in a
sample, or a slot value listed twice.

    python alexa/build_model.py
"""
import json
import re
from pathlib import Path

INVOCATION = "the energy portal"


def intent(name, samples, slots=None):
    d = {"name": name}
    if slots:
        d["slots"] = [{"name": n, "type": t} for n, t in slots]
    d["samples"] = samples
    return d


def slot_type(name, values):
    return {"name": name,
            "values": [{"name": {"value": v, "synonyms": s}} for v, s in values]}


PERIOD = [("period", "COST_PERIOD")]

INTENTS = [
    {"name": "AMAZON.CancelIntent", "samples": []},
    {"name": "AMAZON.HelpIntent", "samples": ["what can I ask", "what can you do", "what can I say"]},
    {"name": "AMAZON.StopIntent", "samples": []},
    {"name": "AMAZON.NavigateHomeIntent", "samples": []},
    {"name": "AMAZON.FallbackIntent", "samples": []},
    {"name": "AMAZON.YesIntent", "samples": []},
    {"name": "AMAZON.NoIntent", "samples": []},

    # ── Live ──────────────────────────────────────────────────────────────────
    intent("SummaryIntent", ["how are we doing", "how's the house", "give me a summary",
                             "what's happening", "what's going on"]),
    intent("BatteryIntent", ["what's the battery at", "what is the battery at",
                             "how much is in the battery", "how much charge is in the battery",
                             "battery level", "how's the battery", "is the battery charging"]),
    intent("SolarIntent", ["how much solar", "how much solar are we making",
                           "what's the solar doing", "how are the panels doing",
                           "how much solar today", "solar"]),
    intent("HomeIntent", ["how much power is the house using", "what's the house using",
                          "how much are we using", "house usage", "what's the load"]),
    intent("GridIntent", ["are we importing", "are we exporting", "what's the grid doing",
                          "how much have we imported", "how much have we exported", "grid"]),
    intent("TodayIntent", ["how was today", "what about today", "today's summary",
                           "today's energy"]),

    # ── Money: cost (money spent), earnings (export income), savings ──────────
    intent("CostIntent", [
        "what did {period} cost", "what has {period} cost", "how much did {period} cost",
        "how much has {period} cost", "what's {period} cost us", "what has {period} cost us",
        "{period} cost", "how much did we spend {period}", "how much have we spent {period}",
        "what did we spend {period}", "what have we spent {period}",
        "how much have we paid {period}", "how much did we pay {period}",
        "what are the costs for {period}", "what were the costs {period}",
        "cost breakdown for {period}", "give me the cost breakdown for {period}",
        "give me the costs for {period}", "the costs for {period}", "costs {period}",
        "{period} costs", "what's the bill {period}", "what's the bill for {period}",
        "what's our electric bill {period}", "what's the electric bill for {period}",
        "what's the damage {period}", "what's the running total {period}",
        "what's the bill", "what's the damage", "what's the running total",
        "how much have we spent", "how much have we paid", "cost breakdown",
        "what are the costs"], PERIOD),
    intent("EarningsIntent", [
        "how much did we earn {period}", "how much did we earn from exporting {period}",
        "how much have we earned {period}", "how much have we earned from exporting {period}",
        "what did exports earn {period}", "what did we make from exports {period}",
        "how much did we earn", "how much have we earned", "what have exports earned"], PERIOD),
    intent("SavingsIntent", [
        "how much has solar saved us {period}", "how much has solar saved us",
        "how much has the battery saved us {period}", "how much has the battery saved us",
        "how much have we saved {period}", "how much have we saved",
        "what have the panels saved us {period}", "what have the panels saved us"], PERIOD),
    intent("YearReviewIntent", [
        "what's our estimated annual cost", "what will this year cost us",
        "what's the estimated cost for the year", "give me the year in review",
        "read me the year in review", "how's the year going"]),
    intent("SolarSplitIntent", [
        "how much of today's solar did we use ourselves",
        "how much of our solar did we use {period}", "how much of our solar did we use",
        "what percentage of our power came from solar {period}",
        "what percentage of our power came from solar", "how self sufficient are we {period}",
        "how self sufficient are we", "where did our solar go {period}",
        "where did our solar go", "solar split {period}"], PERIOD),
    intent("CompareIntent", [
        "how does {period} compare to {versus}", "how does {period} compare",
        "how did {period} compare to {versus}", "compare {period} with {versus}",
        "compare {period} to {versus}", "compare {period}", "was {period} better than {versus}",
        "how does today compare to yesterday", "was today better than yesterday"],
        [("period", "COST_PERIOD"), ("versus", "COST_PERIOD")]),
    intent("RecordsIntent", [
        "what was our {record} {period}", "what was the {record} {period}",
        "what was our {record}", "what was the {record}", "when was our {record} {period}",
        "when was the {record} {period}", "when was our {record}",
        "when did we use the most power {period}", "when did we use the most power"],
        [("record", "RECORD_KIND"), ("period", "COST_PERIOD")]),
    # "What about last month?" - the previous question again, for another period
    intent("PeriodOnlyIntent", ["{period}", "what about {period}", "how about {period}",
                                "and {period}", "and what about {period}"], PERIOD),

    # ── Tariff: the price per unit ────────────────────────────────────────────
    intent("TariffIntent", [
        "what's the {rate} right now", "what's the current {rate}", "what's the {rate}",
        "what is the {rate}", "what's the electricity rate right now",
        "what's the current agile price", "is power cheap at the moment",
        "is electricity cheap right now", "is power expensive right now",
        "how much is a unit right now", "what tariff are we on", "what's our tariff"],
        [("rate", "RATE_KIND")]),
    intent("CheapestTimeIntent", [
        "when is the cheapest time to charge {when}", "when is the cheapest time to charge",
        "when is power cheapest {when}", "when is power cheapest",
        "when is electricity cheapest {when}", "when is electricity cheapest",
        "when's the cheapest time to use power {when}", "when's the cheapest time to use power",
        "when are the cheap rates {when}"], [("when", "CHEAP_WHEN")]),
    intent("GoodTimeIntent", [
        "is now a good time to run the {appliance}", "is now a good time to use the {appliance}",
        "is it a good time to put the {appliance} on", "should I run the {appliance} now",
        "is now a good time to use power", "is now a good time to use electricity"],
        [("appliance", "APPLIANCE")]),

    # ── Forecast, weather ─────────────────────────────────────────────────────
    intent("ForecastIntent", [
        "what's the solar forecast for {day}", "what's the solar forecast",
        "is it going to be a good solar day {day}", "is it going to be a good solar day",
        "how much solar are we expecting {day}", "how much solar are we expecting",
        "how much solar will we get {day}", "is tomorrow better than today for solar",
        "solar forecast", "what's the forecast for solar"], [("day", "DAY_NAME")]),
    intent("WeatherIntent", [
        "what's the weather doing at the house", "what's the weather like at the house",
        "what's the temperature outside", "how warm is it outside", "how cold is it outside",
        "is it windy outside"]),

    # ── Battery, inverter, system ─────────────────────────────────────────────
    intent("PredictiveChargeIntent", [
        "what's the battery set to charge to tonight", "how much will we charge overnight",
        "how much will the battery charge tonight", "is predictive charging switched on",
        "is predictive charging on", "what's the overnight charge target"]),
    intent("BatteryDetailIntent", [
        "how healthy is the battery", "what's the battery {detail}",
        "what is the battery {detail}", "battery {detail}",
        "how many cycles has the battery done", "how much usable capacity do we have left",
        "how much usable energy is left", "how hot is the battery"],
        [("detail", "BATTERY_DETAIL")]),
    intent("BatteryRuntimeIntent", [
        "how long will the battery last", "will the battery get us through the evening",
        "will the battery last the night", "will the battery last until morning",
        "when will the battery be {goal}", "how long until the battery is {goal}",
        "how long till the battery is {goal}", "when will we run out of battery"],
        [("goal", "RUNTIME_GOAL")]),
    intent("InverterStatusIntent", [
        "what {inverter_part} is the inverter in", "are we in eco or storage mode",
        "is the inverter {inverter_part}", "is the {inverter_part} on", "is the grid {inverter_part} OK",
        "what's the grid {inverter_part}", "what is the grid {inverter_part}", "inverter status",
        "how's the inverter", "what's the inverter doing"], [("inverter_part", "INVERTER_ASPECT")]),
    intent("PlugInSolarIntent", [
        "how much is the plug-in solar making",
        "how much of our solar is from the plug-in panels",
        "what are the plug-in panels doing", "how's the plug-in solar"]),
    intent("SchedulerIntent", [
        "what's the scheduler doing tonight", "what's the scheduler doing",
        "what's the next scheduled charge", "are any rules active at the moment",
        "is the scheduler on", "what's scheduled tonight"]),
    intent("SystemHealthIntent", [
        "is everything working", "is everything OK", "any problems with the system",
        "are there any problems", "when was the last {system_part}", "is there an {system_part} available",
        "did we lose {system_part} to the inverter today", "what's in the activity {system_part}",
        "read me the activity {system_part}"], [("system_part", "HEALTH_ASPECT")]),
    intent("ShowViewIntent", [
        "show {view}", "show me {view}", "show the {view}", "show me the {view}",
        "go to {view}", "go to the {view}", "open {view}", "open the {view}"],
        [("view", "VIEW_NAME")]),

    # ── Controls: each one asks "Are you sure?" first ─────────────────────────
    intent("ModeIntent", [
        "switch to {mode} mode", "change to {mode} mode", "put the inverter in {mode} mode",
        "set the inverter to {mode} mode", "{mode} mode"], [("mode", "MODE_NAME")]),
    intent("ExportIntent", [
        "switch to {export_mode}", "switch the battery to {export_mode}",
        "set the battery to {export_mode}", "put the battery on {export_mode}",
        "turn on export"], [("export_mode", "EXPORT_MODE")]),
    intent("ReserveIntent", [
        "set the battery reserve to {percent} percent", "set the reserve to {percent} percent",
        "change the battery reserve to {percent} percent",
        "change the reserve to {percent} percent", "set the battery reserve",
        "{percent} percent"], [("percent", "AMAZON.NUMBER")]),
    intent("HoldChargeIntent", [
        "hold the battery charge until {until}", "hold the charge until {until}",
        "hold the battery until {until}", "keep the battery charge until {until}",
        "hold the battery charge", "until {until}"], [("until", "AMAZON.TIME")]),
    intent("SchedulerToggleIntent", [
        "turn the scheduler {state}", "switch the scheduler {state}",
        "turn {state} the scheduler", "switch {state} the scheduler"], [("state", "ON_OFF")]),
    intent("ClockSyncIntent", ["sync the inverter clock", "set the inverter clock",
                               "update the inverter time", "sync the clock"]),
    intent("QuickChargeIntent", ["charge the battery", "start charging", "start a quick charge",
                                 "quick charge", "boost the battery"]),
    intent("QuickDischargeIntent", ["discharge the battery", "start discharging",
                                    "start a quick discharge", "quick discharge",
                                    "export the battery"]),
    intent("CancelQuickChargeIntent", ["stop charging", "cancel the charge",
                                       "cancel quick charge", "stop the quick charge"]),
    intent("CancelQuickDischargeIntent", ["stop discharging", "cancel the discharge",
                                          "cancel quick discharge", "stop the quick discharge"]),
]

TYPES = [
    # Canonical values must match _ALEXA_PERIOD_BASE in dashboard_server.py. No
    # bare "the week" / "the month": they clash with phrases like "for the week".
    slot_type("COST_PERIOD", [
        ("today", ["so far today", "for today", "today so far"]),
        ("yesterday", ["for yesterday", "yesterday's"]),
        ("this week", ["so far this week", "this week so far", "the past week",
                       "the last seven days", "the week so far", "week to date",
                       "this week's"]),
        ("last week", ["last week's", "the previous week", "previous week"]),
        ("this month", ["so far this month", "this month so far", "month to date",
                        "the month so far", "the past month", "the last thirty days",
                        "this month's"]),
        ("last month", ["last month's", "the previous month", "previous month"]),
        ("this year", ["so far this year", "this year so far", "year to date",
                       "the year so far", "this calendar year", "this year's"]),
        ("last year", ["last year's", "the previous year", "previous year"]),
    ]),
    slot_type("VIEW_NAME", [
        ("live", ["live view", "home", "dashboard", "flow"]),
        ("history", ["graph", "graphs", "chart"]),
        ("costs", ["cost", "money"]),
        ("solar split", ["solar breakdown"]),
        ("tariff", ["rates"]),
        ("hourly", ["power graph", "today"]),
        ("year in review", ["year"]),
        ("forecast", ["solar forecast", "the forecast"]),
        ("battery", ["battery detail", "battery cells", "battery health"]),
        ("schedule", ["scheduler", "schedules"]),
    ]),
    slot_type("RECORD_KIND", [
        ("best solar day", ["sunniest day", "biggest solar day", "best day for solar"]),
        ("worst solar day", ["dullest day", "lowest solar day", "worst day for solar"]),
        ("highest usage day", ["busiest day", "biggest usage day", "day we used the most"]),
        ("peak usage", ["highest house usage", "peak load", "highest demand",
                        "most power used"]),
        ("peak solar", ["highest solar", "peak generation", "most solar power"]),
    ]),
    slot_type("RATE_KIND", [
        ("import", ["electricity rate", "unit rate", "import rate", "electricity price",
                    "agile price", "price", "rate"]),
        ("export", ["export rate", "export price"]),
        ("standing charge", ["daily charge", "standing charges"]),
        ("tariff", ["tariff name"]),
    ]),
    slot_type("CHEAP_WHEN", [("today", ["this afternoon"]),
                             ("tonight", ["overnight", "this evening"])]),
    slot_type("APPLIANCE", [(v, []) for v in (
        "washing machine", "dishwasher", "tumble dryer", "dryer", "oven", "immersion",
        "car charger", "kettle")]),
    slot_type("DAY_NAME", [("today", []), ("tomorrow", [])]),
    slot_type("BATTERY_DETAIL", [
        ("health", ["condition", "state of health"]),
        ("temperature", ["temp"]),
        ("cycles", ["cycle count", "charge cycles"]),
        ("capacity", ["usable capacity", "usable energy"]),
    ]),
    slot_type("RUNTIME_GOAL", [("full", ["charged", "fully charged"]),
                               ("empty", ["flat", "run out"])]),
    slot_type("INVERTER_ASPECT", [
        ("mode", []), ("online", ["connected"]),
        ("backup", ["backup circuit", "backup power", "eps"]),
        ("voltage", []), ("frequency", []),
    ]),
    slot_type("HEALTH_ASPECT", [("backup", []), ("update", []), ("connection", []),
                                ("log", [])]),
    slot_type("MODE_NAME", [("eco", ["dynamic", "eco mode"]),
                            ("storage", ["storage mode"])]),
    slot_type("EXPORT_MODE", [("export", ["max power", "full export", "export mode"]),
                              ("demand only", ["demand", "no export", "demand mode"])]),
    slot_type("ON_OFF", [("on", ["back on"]), ("off", [])]),
]


# Slot names the console refuses (it only says so after a build)
RESERVED_SLOT_NAMES = {"other"}


def check(intents, types):
    seen, slot_types = {}, {}
    for i in intents:
        declared = {s["name"] for s in i.get("slots", [])}
        for s in i.get("slots", []):
            assert s["name"] not in RESERVED_SLOT_NAMES, f"{i['name']}: slot name '{s['name']}' is reserved"
            # The console wants one slot type per slot name across the whole model
            first = slot_types.setdefault(s["name"], (s["type"], i["name"]))
            assert first[0] == s["type"], (f"slot '{s['name']}' is {first[0]} in {first[1]} "
                                           f"but {s['type']} in {i['name']}")
        for s in i["samples"]:
            assert s not in seen, f"'{s}' is in both {seen[s]} and {i['name']}"
            seen[s] = i["name"]
            for slot in re.findall(r"\{(\w+)\}", s):
                assert slot in declared, f"{i['name']}: '{s}' uses undeclared {{{slot}}}"
            assert not re.search(r"\d", s), f"'{s}': spell numbers out"
    for t in types:
        words = [w for v in t["values"] for w in [v["name"]["value"]] + v["name"]["synonyms"]]
        dupes = {w for w in words if words.count(w) > 1}
        assert not dupes, f"{t['name']} lists {dupes} twice"


if __name__ == "__main__":
    check(INTENTS, TYPES)
    model = {"interactionModel": {"languageModel": {
        "invocationName": INVOCATION, "intents": INTENTS, "types": TYPES}}}
    out = Path(__file__).with_name("interaction-model.en-GB.json")
    out.write_text(json.dumps(model, indent=2) + "\n", encoding="utf-8")
    print(f"{out.name}: {len(INTENTS)} intents, "
          f"{sum(len(i['samples']) for i in INTENTS)} samples")
