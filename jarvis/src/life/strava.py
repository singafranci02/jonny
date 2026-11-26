"""Strava → Life HQ: workouts reach the Fitness logs by themselves.

Ported from MacroClaw (MacroClawAgent/macroclawagent): lib/strava.ts (OAuth,
token refresh, fetching), src/strava/features.ts (per-day training load) and
src/nutrition/targets.ts (deterministic daily fuel targets). As in MacroClaw,
only activity summaries are kept (sport, time, distance, pace or speed,
average heart rate, climb, name); GPS, streams and splits are never fetched.

Setup: a Strava API app (strava.com/settings/api, callback domain
"localhost") with STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in .env, then
/strava on Telegram. Tokens live in data/strava.json (owner-only, gitignored,
outside life.db so they never reach the iCloud backups).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from ..tools import Tool

AUTH_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
API = "https://www.strava.com/api/v3"
# Strava always allows localhost: the browser lands on a dead page whose
# address carries the code, and he pastes that address to the bot
REDIRECT = "http://localhost/exchange_token"
SCOPE = "read,activity:read_all"
SYNC_EVERY = timedelta(minutes=30)

RUN_TYPES = {"Run", "TrailRun", "VirtualRun", "Hike", "Walk"}
SPORT_LABEL = {
    "Run": "Run", "TrailRun": "Trail run", "VirtualRun": "Treadmill run", "Ride": "Ride",
    "VirtualRide": "Indoor ride", "MountainBikeRide": "MTB ride", "GravelRide": "Gravel ride",
    "EBikeRide": "E-bike ride", "Swim": "Swim", "OpenWaterSwim": "Open-water swim", "Walk": "Walk",
    "Hike": "Hike", "WeightTraining": "Strength", "Workout": "Workout", "Yoga": "Yoga",
    "Crossfit": "CrossFit", "HighIntensityIntervalTraining": "HIIT", "Rowing": "Row",
    "Tennis": "Tennis", "Padel": "Padel", "Soccer": "Football",
}


class StravaError(RuntimeError):
    pass


def _sport(a: dict) -> str:
    return a.get("sport_type") or a.get("type") or "Workout"


def sport_label(sport: str) -> str:
    return SPORT_LABEL.get(sport) or re.sub(r"(?<!^)(?=[A-Z])", " ", sport).capitalize()


def _pace(seconds_per_km: float) -> str:
    m, s = divmod(int(round(seconds_per_km)), 60)
    return f"{m}:{s:02d}"


def workout_note(a: dict) -> str:
    """One line per activity, e.g. 'Run: 8.1 km, 5:52/km, avg HR 151, 64 m up. "Morning Run"'."""
    sport = _sport(a)
    km = (a.get("distance") or 0) / 1000
    speed = a.get("average_speed") or 0
    bits = []
    if km >= 0.1:
        bits.append(f"{km:.1f} km")
        if sport in RUN_TYPES and speed > 0:
            bits.append(f"{_pace(1000 / speed)}/km")
        elif sport in ("Swim", "OpenWaterSwim") and speed > 0:
            bits.append(f"{_pace(100 / speed)}/100m")
        elif speed > 0:
            bits.append(f"{speed * 3.6:.1f} km/h")
    if a.get("average_heartrate"):
        bits.append(f"avg HR {round(a['average_heartrate'])}")
    if (a.get("total_elevation_gain") or 0) >= 10:
        bits.append(f"{round(a['total_elevation_gain'])} m up")
    name = " ".join(str(a.get("name") or "").split())[:80]
    text = sport_label(sport) + (": " + ", ".join(bits) if bits else "")
    return text + (f'. "{name}"' if name else ".")


# ---------------------------------------------------------------- training load (MacroClaw features.ts)


def intensity(avg_hr: float | None, avg_speed: float | None, minutes: float, runlike: bool) -> int:
    """Coarse 0-10 effort from averages only. MacroClaw's rule, except pace
    only counts for run-like sports (a ride's speed isn't a running pace)."""
    score = 5
    if avg_hr:
        score += -2 if avg_hr < 120 else -1 if avg_hr < 140 else 0 if avg_hr < 160 else 1 if avg_hr < 170 else 2
    if runlike and avg_speed:
        min_per_km = (1000 / avg_speed) / 60 if avg_speed > 0 else 10
        score += 2 if min_per_km < 4.5 else 1 if min_per_km < 5.5 else -1 if min_per_km > 7.5 else 0
    if minutes > 90:
        score += 1
    return max(0, min(10, round(score)))


def day_features(activities: list[dict]) -> dict[str, dict]:
    """Per local day: minutes, run minutes, km, average intensity, long run."""
    days: dict[str, dict] = defaultdict(lambda: {"minutes": 0, "run_minutes": 0, "km": 0.0, "scores": [],
                                                 "long_run": False, "sessions": 0})
    for a in activities:
        start = a.get("start_date_local") or a.get("start_date")
        if not start:
            continue
        d = days[start[:10]]
        mins = round((a.get("moving_time") or a.get("elapsed_time") or 0) / 60)
        km = (a.get("distance") or 0) / 1000
        runlike = _sport(a) in RUN_TYPES
        d["minutes"] += mins
        d["km"] += km
        d["sessions"] += 1
        if runlike:
            d["run_minutes"] += mins
            if mins > 75 or km > 15:
                d["long_run"] = True
        d["scores"].append(intensity(a.get("average_heartrate"), a.get("average_speed"), mins, runlike))
    out = {}
    for day, d in days.items():
        scores = d.pop("scores")
        d["intensity"] = round(sum(scores) / len(scores)) if scores else 0
        d["km"] = round(d["km"], 1)
        out[day] = d
    return out


def load_trend(features: dict[str, dict], today: date) -> tuple[int, float, float]:
    """(+1 up / 0 steady / -1 down, this week's and last week's average daily
    minutes): MacroClaw's ±15% rule."""
    def avg(lo: int, hi: int) -> float:
        return sum(f["minutes"] for d, f in features.items()
                   if today - timedelta(days=hi) < date.fromisoformat(d) <= today - timedelta(days=lo)) / 7
    this, last = avg(0, 7), avg(7, 14)
    if last == 0:
        return (1 if this > 0 else 0), this, last
    delta = (this - last) / last
    return (1 if delta > 0.15 else -1 if delta < -0.15 else 0), this, last


# ---------------------------------------------------------------- fuel (MacroClaw targets.ts)

PROTEIN_RATIO = {"performance": 2.0, "lean_bulk": 1.8, "cut": 2.2}
PROTEIN_DEFAULT_G = {"performance": 160, "lean_bulk": 150, "cut": 170}
BASE_KCAL_DEFAULT = {"performance": 2800, "lean_bulk": 2600, "cut": 2000}


def base_calories(goal: str, weight_kg: float | None) -> int:
    if weight_kg and weight_kg > 0:
        bmr_ref = 10 * 70 + 6.25 * 175 - 5 * 28 + 5  # 70 kg, 175 cm, 28 y reference athlete
        tdee = round(weight_kg / 70 * bmr_ref * 1.55)
        return round(tdee * 0.85) if goal == "cut" else round(tdee * 1.1) if goal == "lean_bulk" else tdee
    return BASE_KCAL_DEFAULT.get(goal, 2500)


def day_targets(goal: str, weight_kg: float | None, feature: dict | None) -> dict:
    adj = 0
    if feature and feature.get("minutes"):
        adj += 500 if feature["intensity"] >= 7 else 300 if feature["intensity"] >= 4 else 0
        adj += 200 if feature.get("long_run") else 0
    kcal = base_calories(goal, weight_kg) + adj
    if weight_kg and weight_kg > 0:
        protein = round(min(weight_kg * 2.2, max(weight_kg * 1.6, weight_kg * PROTEIN_RATIO.get(goal, 1.8))))
    else:
        protein = PROTEIN_DEFAULT_G.get(goal, 150)
    fat = round(max(weight_kg * 0.6 if weight_kg else 0, kcal * 0.2 / 9, kcal * 0.27 / 9))
    carbs = round(max(0, (kcal - protein * 4 - fat * 9) / 4))
    out = {"kcal": kcal, "protein_g": protein, "fat_g": fat, "carbs_g": carbs}
    if feature and feature.get("minutes") and feature.get("intensity"):
        out["timing"] = (f"before: {round(40 + feature['intensity'] * 2)} g carbs; "
                         "after: 30 g protein + 50 g carbs")
    return out


def _targets_line(t: dict) -> str:
    return f"{t['kcal']:,} kcal · protein {t['protein_g']} g · carbs {t['carbs_g']} g · fat {t['fat_g']} g"


# ---------------------------------------------------------------- the client


class Strava:
    def __init__(self, store, clock, token_path: Path, area: str = "fitness", http=None):
        self.store = store
        self.clock = clock
        self.token_path = Path(token_path)
        self.area = area
        self._http = http

    # -- plumbing

    def http(self):
        if self._http is None:
            import httpx

            self._http = httpx.Client(timeout=20)
        return self._http

    @staticmethod
    def keys() -> tuple[str, str] | None:
        cid = os.environ.get("STRAVA_CLIENT_ID", "").strip()
        secret = os.environ.get("STRAVA_CLIENT_SECRET", "").strip()
        return (cid, secret) if cid and secret else None

    def tokens(self) -> dict | None:
        try:
            return json.loads(self.token_path.read_text())
        except (OSError, ValueError):
            return None

    def _save(self, tok: dict) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(tok, f)
        tmp.replace(self.token_path)

    def connected(self) -> bool:
        return bool(self.keys() and self.tokens())

    def _token_call(self, **data) -> dict:
        keys = self.keys()
        if not keys:
            raise StravaError("Strava keys are missing: STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in .env")
        r = self.http().post(TOKEN_URL, data={"client_id": keys[0], "client_secret": keys[1], **data})
        if r.status_code != 200:
            raise StravaError(f"Strava said {r.status_code}: {r.text[:200]}")
        return r.json()

    def access_token(self) -> str:
        tok = self.tokens()
        if not tok:
            raise StravaError("Strava isn't connected yet: send /strava to the bot")
        if tok.get("expires_at", 0) - time.time() < 300:  # refresh 5 min early, as MacroClaw does
            new = self._token_call(grant_type="refresh_token", refresh_token=tok["refresh_token"])
            tok.update(access_token=new["access_token"], refresh_token=new["refresh_token"],
                       expires_at=new["expires_at"])
            self._save(tok)
        return tok["access_token"]

    # -- connecting

    def connect_url(self) -> str:
        keys = self.keys()
        if not keys:
            raise StravaError("Strava keys are missing")
        state = secrets.token_urlsafe(12)
        self.store.set_setting("strava_state", state)
        return AUTH_URL + "?" + urlencode({
            "client_id": keys[0], "redirect_uri": REDIRECT, "response_type": "code",
            "approval_prompt": "auto", "scope": SCOPE, "state": state,
        })

    @staticmethod
    def looks_like_callback(text: str) -> bool:
        return "exchange_token" in text and "code=" in text

    def finish_connect(self, pasted: str) -> str:
        """Exchange the code from the pasted localhost address for tokens."""
        m = re.search(r"\S*exchange_token\S*", pasted)
        q = parse_qs(urlparse(m.group(0) if m else pasted).query)
        code = (q.get("code") or [""])[0]
        if not code:
            raise StravaError("that address has no code in it; tap the link again")
        expected = self.store.get_setting("strava_state")
        if not expected or (q.get("state") or [""])[0] != expected:
            raise StravaError("that link is old or not from me; send /strava for a fresh one")
        scope = (q.get("scope") or [""])[0]
        if "activity:read" not in scope:
            raise StravaError("Strava didn't grant activity access; tap the link again and leave "
                              "\"View data about your activities\" ticked")
        tok = self._token_call(grant_type="authorization_code", code=code)
        athlete = tok.get("athlete") or {}
        self._save({"access_token": tok["access_token"], "refresh_token": tok["refresh_token"],
                    "expires_at": tok["expires_at"], "scope": scope,
                    "athlete": {"id": athlete.get("id"), "firstname": athlete.get("firstname")}})
        self.store.set_setting("strava_state", None)
        self.store.set_setting("strava_after", None)
        return athlete.get("firstname") or "you"

    # -- data

    def activities(self, after: int, pages: int = 5) -> list[dict]:
        token = self.access_token()
        out: list[dict] = []
        for page in range(1, pages + 1):
            r = self.http().get(f"{API}/athlete/activities", params={"after": after, "per_page": 100, "page": page},
                                headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 429:
                raise StravaError("Strava's rate limit is hit; try again in 15 minutes")
            if r.status_code != 200:
                raise StravaError(f"Strava said {r.status_code}: {r.text[:200]}")
            batch = r.json()
            out.extend(batch)
            if len(batch) < 100:
                break
        return out

    def sync(self, days: int | None = None) -> str:
        """New Strava activities become Fitness "workout" logs (minutes as
        the value, the summary as the note), each exactly once."""
        last = self.store.get_setting("strava_after")
        if days:
            after = int(time.time()) - int(days) * 86400
        elif last:
            after = int(last) - 3 * 86400  # late uploads and edits: dedupe makes re-reading free
        else:
            after = int(time.time()) - 30 * 86400
        acts = self.activities(after)
        new = []
        newest = int(last or 0)
        for a in sorted(acts, key=lambda a: a.get("start_date") or ""):
            start = datetime.fromisoformat(a["start_date"].replace("Z", "+00:00"))
            newest = max(newest, int(start.timestamp()))
            row = self.store.add_log_once(
                f"strava:{a['id']}", self.area, "workout", round((a.get("moving_time") or 0) / 60), "min",
                workout_note(a), start.astimezone(timezone.utc),
            )
            if row:
                new.append(f"{self.clock.when(row['logged_at'])} {round(row['value'])} min {row['note']}")
        if newest:
            self.store.set_setting("strava_after", newest)
        self.store.set_setting("strava_last_sync", datetime.now(timezone.utc).isoformat())
        if not new:
            return f"Strava synced: nothing new ({len(acts)} activities checked)."
        return f"Strava synced: {len(new)} new workout{'s' if len(new) != 1 else ''} logged.\n" + "\n".join(new[-15:])

    def training_load(self, days: int = 14) -> str:
        days = max(7, min(int(days or 14), 60))
        today = self.clock.today()
        acts = self.activities(int(time.time()) - (max(days, 14) + 1) * 86400)
        feats = day_features(acts)
        trend, this, last = load_trend(feats, today)
        window = {d: f for d, f in feats.items() if date.fromisoformat(d) > today - timedelta(days=days)}
        mins = sum(f["minutes"] for f in window.values())
        km = sum(f["km"] for f in window.values())
        n = sum(f["sessions"] for f in window.values())
        word = {1: "up", 0: "steady", -1: "down"}[trend]
        lines = [f"Last {days} days (Strava): {n} sessions, {mins // 60}h {mins % 60:02d}m, {km:.1f} km.",
                 f"Load {word}: {this:.0f} min/day this week vs {last:.0f} last week."]
        for d in sorted(window, reverse=True):
            f = window[d]
            extra = f", runs {f['run_minutes']} min" if f["run_minutes"] and f["run_minutes"] != f["minutes"] else ""
            lines.append(f"{date.fromisoformat(d):%a %d %b}: {f['minutes']} min{extra}, {f['km']} km, "
                         f"intensity {f['intensity']}/10" + (", long run" if f["long_run"] else ""))
        return "\n".join(lines)

    def latest_weight(self) -> tuple[float, str] | None:
        rows = self.store.get_logs(None, "weight", 365, limit=1)
        if rows and rows[0]["value"]:
            return float(rows[0]["value"]), self.clock.when(rows[0]["logged_at"])
        return None

    def fuel_targets(self, goal: str = "performance", weight_kg: float | None = None) -> str:
        goal = goal if goal in PROTEIN_RATIO else "performance"
        source = "given"
        if not weight_kg:
            w = self.latest_weight()
            if w:
                weight_kg, source = w[0], f"logged {w[1]}"
        today_feature = None
        if self.connected():
            today_feature = day_features(self.activities(int(time.time()) - 86400)).get(
                self.clock.today().isoformat())
        rest = day_targets(goal, weight_kg, None)
        moderate = day_targets(goal, weight_kg, {"minutes": 45, "intensity": 5})
        hard = day_targets(goal, weight_kg, {"minutes": 60, "intensity": 7})
        lines = [f"Goal {goal}, " + (f"weight {weight_kg:g} kg ({source})." if weight_kg
                                     else "no weight logged, so reference values (log a weight for better ones)."),
                 f"Rest day: {_targets_line(rest)}",
                 f"Moderate session: {_targets_line(moderate)}",
                 f"Hard session (intensity 7+): {_targets_line(hard)}",
                 "Long run (over 75 min or 15 km): +200 kcal on top, as carbs."]
        if today_feature:
            t = day_targets(goal, weight_kg, today_feature)
            lines.append(f"Today ({today_feature['minutes']} min, intensity {today_feature['intensity']}/10): "
                         f"{_targets_line(t)}" + (f"; {t['timing']}" if t.get("timing") else ""))
        lines.append("Estimates from a simple formula (MacroClaw's), not medical advice.")
        return "\n".join(lines)


def build_strava_tools(strava: Strava | None) -> list[Tool]:
    if strava is None:
        return []

    def needs_connection(fn):
        def run(**kw):
            if not strava.keys():
                return ("Strava isn't set up: it needs STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in Jarvis's .env "
                        "(from strava.com/settings/api), then /strava on Telegram.")
            if not strava.tokens():
                return "Strava isn't connected yet: tell him to send /strava to the bot (one tap, one paste)."
            return fn(**kw)
        return run

    return [
        Tool(
            name="strava_sync",
            description=("Pull new Strava activities into the Fitness logs now (they also sync every 30 minutes "
                         "by themselves). Use before reviewing training if the latest session might be missing."),
            parameters={"type": "object", "properties": {
                "days": {"type": "integer", "description": "optional: re-read this many days back"}}},
            func=needs_connection(lambda days=None: strava.sync(days)),
            timeout=45,
        ),
        Tool(
            name="training_load",
            description=("Training load from Strava: sessions, time and distance per day, a 0-10 intensity per "
                         "day, long runs, and whether load is going up, steady or down week on week. Use it when "
                         "planning or reviewing training."),
            parameters={"type": "object", "properties": {
                "days": {"type": "integer", "description": "window, 7-60, default 14"}}},
            func=needs_connection(lambda days=14: strava.training_load(days)),
            timeout=45,
        ),
        Tool(
            name="fuel_targets",
            description=("Daily calorie and macro targets (protein, carbs, fat) for rest, moderate and hard "
                         "training days, plus today's from his Strava training, with pre/post-workout carbs. "
                         "Estimates; weight comes from his latest weight log unless given."),
            parameters={"type": "object", "properties": {
                "goal": {"type": "string", "enum": ["performance", "lean_bulk", "cut"],
                         "description": "default performance (maintenance)"},
                "weight_kg": {"type": "number"}}},
            func=lambda goal="performance", weight_kg=None: strava.fuel_targets(goal, weight_kg),
            timeout=45,
        ),
    ]
