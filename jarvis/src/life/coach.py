"""The coach: a training program to follow, not just a log.

Built for physique (muscle and leanness) with running for conditioning,
fed by Strava. Deterministic, no model calls, so a tap is instant and the
rules are the same every time:

- Program: a lifting split picked from his days per week (full body,
  upper/lower, push/pull/legs) plus runs (easy, intervals, long), placed on
  the week so hard days don't pile up.
- Blocks: 4 weeks. Weeks 1-3 build (reps in reserve 3, 2, 1; a set more on
  the main lifts in week 3), week 4 deloads (fewer sets, lighter, easy).
- Lifts: double progression per exercise. Each has a rep range; "hit" adds
  a rep until the top of the range, then the weight goes up and reps reset;
  "easy" jumps faster; two misses in a row step the weight back 10%.
- Runs: the weekly running time starts from his real Strava average and
  grows 10% a week inside a block (deload: 70%); each new block re-bases on
  what he actually ran. Easy paces come from his own Strava runs.
- Readiness: last night's sleep, mood and the training-load ratio (last 7
  days against the 4-week average) turn a day green, amber or red; amber
  swaps intervals for an easy run, red also trims a set from each lift.
- Physique loop: the weekly review compares his bodyweight trend with the
  goal's target rate and moves the calorie adjustment that fuel_targets uses.

Tracking: on Telegram, each exercise gets ✓ (hit) / ⤴ (easy) / ↓ (missed) /
⚖ (set the weight); Finish applies the progression. Runs are ticked off by
the Strava sync. Data: tables coach_sessions and coach_lifts in life.db, and
the profile in settings (coach_profile).
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass
from datetime import date, timedelta

from ..tools import Tool
from .clock import to_iso, utc_now

# ---------------------------------------------------------------- library


@dataclass(frozen=True)
class Exercise:
    key: str
    name: str
    lo: int  # rep range
    hi: int
    inc: float | None  # kg per step; None = bodyweight (progress by reps)
    short: str  # button label


def _ex(key, name, lo, hi, inc, short=None):
    return Exercise(key, name, lo, hi, inc, short or name.split(" (")[0][:12])


LIB = {e.key: e for e in [
    _ex("bench", "Bench press", 6, 10, 2.5, "Bench"),
    _ex("db_bench", "Dumbbell bench press", 8, 12, 2, "DB bench"),
    _ex("pushup", "Push-up", 8, 20, None),
    _ex("incline_db", "Incline dumbbell press", 8, 12, 2, "Incline DB"),
    _ex("decline_pushup", "Feet-up push-up", 8, 15, None, "Feet-up PU"),
    _ex("ohp", "Overhead press", 6, 10, 2.5, "OHP"),
    _ex("db_ohp", "Dumbbell shoulder press", 8, 12, 2, "DB press"),
    _ex("pike_pushup", "Pike push-up", 6, 12, None, "Pike PU"),
    _ex("row", "Barbell row", 6, 10, 2.5, "Row"),
    _ex("db_row", "One-arm dumbbell row", 8, 12, 2, "DB row"),
    _ex("inverted_row", "Inverted row", 8, 15, None, "Inv. row"),
    _ex("pulldown", "Lat pulldown", 8, 12, 2.5, "Pulldown"),
    _ex("pullup", "Pull-up", 5, 10, None, "Pull-up"),
    _ex("cable_row", "Seated cable row", 10, 12, 2.5, "Cable row"),
    _ex("squat", "Back squat", 5, 8, 5, "Squat"),
    _ex("goblet", "Goblet squat", 8, 15, 2, "Goblet"),
    _ex("rdl", "Romanian deadlift", 8, 10, 5, "RDL"),
    _ex("db_rdl", "Dumbbell Romanian deadlift", 8, 12, 2, "DB RDL"),
    _ex("sl_rdl", "Single-leg Romanian deadlift", 10, 15, None, "SL RDL"),
    _ex("deadlift", "Trap-bar deadlift", 4, 6, 5, "Deadlift"),
    _ex("hip_thrust", "Hip thrust", 8, 15, None, "Hip thrust"),
    _ex("split_squat", "Bulgarian split squat", 8, 12, 2, "Split squat"),
    _ex("bw_split_squat", "Bulgarian split squat (bodyweight)", 10, 20, None, "Split squat"),
    _ex("leg_press", "Leg press", 10, 15, 10, "Leg press"),
    _ex("step_up", "Dumbbell step-up", 10, 12, 2, "Step-up"),
    _ex("bw_step_up", "Step-up", 12, 20, None),
    _ex("leg_ext", "Leg extension", 12, 15, 2.5, "Leg ext"),
    _ex("leg_curl", "Leg curl", 10, 15, 2.5, "Leg curl"),
    _ex("nordic", "Nordic curl (slow lowering)", 4, 8, None, "Nordic"),
    _ex("calf", "Standing calf raise", 10, 15, 5, "Calves"),
    _ex("db_calf", "Dumbbell calf raise", 12, 20, 2, "Calves"),
    _ex("sl_calf", "Single-leg calf raise", 12, 25, None, "Calves"),
    _ex("lateral", "Lateral raise", 12, 20, 1, "Laterals"),
    _ex("rear_delt", "Rear-delt fly", 15, 20, 1, "Rear delts"),
    _ex("prone_y", "Prone Y-raise", 12, 20, None, "Y-raise"),
    _ex("curl", "EZ-bar curl", 8, 12, 2.5, "Curl"),
    _ex("db_curl", "Dumbbell curl", 10, 15, 1, "Curl"),
    _ex("pushdown", "Triceps pushdown", 10, 15, 2.5, "Pushdown"),
    _ex("oh_ext", "Overhead dumbbell extension", 10, 15, 1, "OH ext"),
    _ex("bench_dip", "Bench dip", 10, 20, None, "Dip"),
    _ex("leg_raise", "Hanging leg raise", 10, 15, None, "Leg raise"),
    _ex("rev_crunch", "Reverse crunch", 12, 20, None, "Rev crunch"),
]}

EQUIPMENT = ("gym", "dumbbells", "bodyweight")
# movement slot -> exercise for gym / dumbbells / bodyweight (None: left out)
SLOTS = {
    "h_push": ("bench", "db_bench", "pushup"),
    "incline": ("incline_db", "incline_db", "decline_pushup"),
    "v_push": ("ohp", "db_ohp", "pike_pushup"),
    "h_pull": ("row", "db_row", "inverted_row"),
    "v_pull": ("pulldown", "pullup", "pullup"),
    "row2": ("cable_row", "db_row", "inverted_row"),
    "squat": ("squat", "goblet", "bw_split_squat"),
    "hinge": ("rdl", "db_rdl", "sl_rdl"),
    "deadlift": ("deadlift", "db_rdl", "hip_thrust"),
    "lunge": ("split_squat", "split_squat", "bw_split_squat"),
    "quad2": ("leg_press", "step_up", "bw_step_up"),
    "knee_ext": ("leg_ext", None, None),
    "leg_curl": ("leg_curl", "nordic", "nordic"),
    "calves": ("calf", "db_calf", "sl_calf"),
    "lateral": ("lateral", "lateral", "prone_y"),
    "rear_delt": ("rear_delt", "rear_delt", "prone_y"),
    "biceps": ("curl", "db_curl", None),
    "triceps": ("pushdown", "oh_ext", "bench_dip"),
    "abs": ("leg_raise", "rev_crunch", "rev_crunch"),
}
# template -> (title, lower-body day?, [(slot, sets)]) in priority order
TEMPLATES = {
    "full_a": ("Full body A", True, [("squat", 3), ("h_push", 3), ("h_pull", 3), ("hinge", 2), ("lateral", 2), ("abs", 2), ("biceps", 2)]),
    "full_b": ("Full body B", True, [("deadlift", 3), ("v_push", 3), ("v_pull", 3), ("lunge", 2), ("triceps", 2), ("calves", 2), ("rear_delt", 2)]),
    "full_c": ("Full body C", True, [("quad2", 3), ("incline", 3), ("row2", 3), ("leg_curl", 2), ("lateral", 2), ("biceps", 2), ("abs", 2)]),
    "upper_a": ("Upper A", False, [("h_push", 3), ("h_pull", 3), ("v_push", 3), ("v_pull", 3), ("lateral", 3), ("biceps", 2), ("triceps", 2)]),
    "lower_a": ("Lower A", True, [("squat", 3), ("hinge", 3), ("quad2", 2), ("leg_curl", 3), ("calves", 3), ("abs", 2)]),
    "upper_b": ("Upper B", False, [("incline", 3), ("v_pull", 3), ("row2", 3), ("lateral", 3), ("rear_delt", 2), ("biceps", 2), ("triceps", 2)]),
    "lower_b": ("Lower B", True, [("deadlift", 3), ("lunge", 3), ("knee_ext", 3), ("leg_curl", 2), ("calves", 3), ("abs", 2)]),
    "push": ("Push", False, [("h_push", 3), ("v_push", 3), ("incline", 3), ("lateral", 3), ("triceps", 3)]),
    "pull": ("Pull", False, [("v_pull", 3), ("h_pull", 3), ("row2", 2), ("rear_delt", 3), ("biceps", 3)]),
    "legs": ("Legs", True, [("squat", 3), ("hinge", 3), ("lunge", 2), ("leg_curl", 3), ("calves", 3), ("abs", 2)]),
}
SPLITS = {
    1: ["full_a"], 2: ["full_a", "full_b"], 3: ["full_a", "full_b", "full_c"],
    4: ["upper_a", "lower_a", "upper_b", "lower_b"], 5: ["upper_a", "lower_a", "push", "pull", "legs"],
    6: ["push", "pull", "legs", "push", "pull", "legs"],
}
LIFT_DAYS = {0: [], 1: [0], 2: [0, 3], 3: [0, 2, 4], 4: [0, 1, 3, 4], 5: [0, 1, 2, 4, 5], 6: [0, 1, 2, 3, 4, 5]}
RUN_KINDS = {0: [], 1: ["long"], 2: ["easy", "long"], 3: ["easy", "intervals", "long"],
             4: ["easy", "intervals", "easy", "long"]}
RUN_TITLE = {"easy": "Easy run", "intervals": "Intervals", "long": "Long run"}
# share of the week's running time per run (each easy run gets the easy share)
RUN_SHARES = {1: {"long": 1.0}, 2: {"easy": 0.45, "long": 0.55}, 3: {"easy": 0.35, "intervals": 0.25, "long": 0.4},
              4: {"easy": 0.25, "intervals": 0.2, "long": 0.3}}
EXERCISES_BY_MINUTES = {45: 5, 60: 6, 75: 7}

BLOCK = 4  # weeks: 3 build + 1 deload
WEEK_RIR = [3, 2, 1, 4]
WEEK_NAME = ["build", "build", "peak", "deload"]
RUN_FACTOR = [1.0, 1.1, 1.2, 0.7]
INTERVALS = ["6 × 2 min hard, 2 min easy jog between", "5 × 3 min hard, 2 min easy between",
             "6 × 3 min hard, 2 min easy between", "4 × 2 min brisk, 2 min easy between"]

# goal -> (label, fuel goal for fuel_targets, target bodyweight change %/week (low, high))
GOALS = {
    "muscle": ("Build muscle", "lean_bulk", (0.1, 0.5)),
    "lean": ("Lean out", "cut", (-1.0, -0.3)),
    "recomp": ("Recomp", "performance", (-0.25, 0.15)),
    "run": ("Run faster", "performance", (-0.3, 0.2)),
}
EXPERIENCE = {"new": "New to lifting", "mid": "1-3 years", "adv": "3+ years"}
DEFAULT_RUN_BASE = {"new": 60, "mid": 90, "adv": 120}  # weekly minutes when Strava has nothing yet
OUTCOMES = {"h": "hit", "e": "easy", "m": "missed"}
MARK = {"hit": "✓", "easy": "⤴", "missed": "↓"}
RUN_SPORTS = ("Run", "Trail run", "Treadmill run")


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _round_to(x: float, step: float) -> float:
    return round(round(x / step) * step, 2)


def _kg(x: float | None) -> str:
    return f"{x:g} kg" if x is not None else ""


def _pace(sec: float) -> str:
    m, s = divmod(int(round(sec)), 60)
    return f"{m}:{s:02d}"


# ---------------------------------------------------------------- the coach


class Coach:
    def __init__(self, store, clock, area: str = "fitness"):
        self.store = store
        self.clock = clock
        self.area = area
        with store._db() as c:
            c.executescript("""
create table if not exists coach_sessions (
  id integer primary key autoincrement,
  day text not null,
  kind text not null,          -- lift | run
  template text not null,      -- upper_a ... | easy | intervals | long
  minutes integer,
  status text not null default 'planned',   -- planned | done | skipped
  results text not null default '{}',       -- lift: exercise -> hit/easy/missed; run: what Strava saw
  completed_at text
);
create index if not exists coach_sessions_day on coach_sessions (day);
create table if not exists coach_lifts (
  exercise text primary key,
  load real,
  reps integer not null,
  misses integer not null default 0,
  history text not null default '[]',
  updated_at text not null
);
""")

    # -------- profile and program

    def profile(self) -> dict | None:
        return self.store.get_setting("coach_profile")

    def _save_profile(self, p: dict) -> None:
        self.store.set_setting("coach_profile", p)

    def setup(self, goal: str, lift_days: int, run_days: int, equipment: str, experience: str,
              minutes: int = 60, age: int | None = None) -> str:
        if goal not in GOALS:
            raise ValueError(f"goal must be one of {', '.join(GOALS)}")
        if equipment not in EQUIPMENT:
            raise ValueError(f"equipment must be one of {', '.join(EQUIPMENT)}")
        if experience not in EXPERIENCE:
            raise ValueError(f"experience must be one of {', '.join(EXPERIENCE)}")
        lift_days, run_days = int(lift_days), int(run_days)
        if not 0 <= lift_days <= 6 or not 0 <= run_days <= 4 or lift_days + run_days == 0:
            raise ValueError("lift_days 0-6 and run_days 0-4, not both 0")
        minutes = min(EXERCISES_BY_MINUTES, key=lambda m: abs(m - int(minutes or 60)))
        today = self.clock.today()
        old = self.profile() or {}
        same_block = old and old.get("lift_days") == lift_days and old.get("run_days") == run_days
        p = {
            "goal": goal, "lift_days": lift_days, "run_days": run_days, "equipment": equipment,
            "experience": experience, "minutes": minutes,
            "age": int(age) if age else old.get("age"),
            "kcal_adjust": old.get("kcal_adjust", 0),
            # a new split starts a new block this week; a tweak keeps the block running
            "start": old["start"] if same_block else monday(today).isoformat(),
            "first_day": old.get("first_day", today.isoformat()) if same_block else today.isoformat(),
            "run_bases": old.get("run_bases", {}) if same_block else {},
        }
        self._save_profile(p)
        with self.store._db() as c:  # future plans follow the new profile
            c.execute("delete from coach_sessions where status = 'planned' and day >= ?", (today.isoformat(),))
        self.plan_week(monday(today))
        return self.overview()

    def set_option(self, **fields) -> str:
        p = self.profile()
        if not p:
            raise ValueError("no program yet: set one up first (coach_setup)")
        merged = {k: p[k] for k in ("goal", "lift_days", "run_days", "equipment", "experience", "minutes", "age")}
        merged.update({k: v for k, v in fields.items() if v is not None})
        return self.setup(**merged)

    def block_week(self, day: date) -> tuple[int, int]:
        """(block number, week in block 0-3) for a day."""
        p = self.profile()
        weeks = (monday(day) - date.fromisoformat(p["start"])).days // 7
        return max(0, weeks) // BLOCK, max(0, weeks) % BLOCK

    def overview(self) -> str:
        p = self.profile()
        if not p:
            return "No program yet."
        split = " / ".join(TEMPLATES[t][0] for t in SPLITS.get(p["lift_days"], []))
        runs = ", ".join(RUN_TITLE[k] for k in RUN_KINDS[p["run_days"]])
        blk, wk = self.block_week(self.clock.today())
        lines = [
            f"Goal: {GOALS[p['goal']][0]} · {EXPERIENCE[p['experience']]} · {p['equipment']} · {p['minutes']} min sessions",
            f"Lifting {p['lift_days']}×/week: {split or 'none'}",
            f"Running {p['run_days']}×/week: {runs or 'none'} (weekly time from your Strava, +10%/week)",
            f"Block {blk + 1}, week {wk + 1} of {BLOCK} ({WEEK_NAME[wk]}). Weeks 1-3 build, week 4 deloads.",
        ]
        return "\n".join(lines)

    # -------- the week's sessions

    def desired_week(self, mon: date) -> list[tuple[str, str, str, int | None]]:
        """(day, kind, template, minutes) the profile asks for in this week."""
        p = self.profile()
        lift_days = LIFT_DAYS[p["lift_days"]]
        templates = SPLITS.get(p["lift_days"], [])
        out = [((mon + timedelta(days=d)).isoformat(), "lift", t, p["minutes"]) for d, t in zip(lift_days, templates)]
        kinds = RUN_KINDS[p["run_days"]]
        if kinds:
            blk, wk = self.block_week(mon)
            weekly = self.run_base(blk) * RUN_FACTOR[wk]
            for day, kind in zip(self._run_days(lift_days, templates, kinds), kinds):
                out.append(((mon + timedelta(days=day)).isoformat(), "run", kind, self._run_minutes(kind, kinds, weekly)))
        return sorted(out)

    @staticmethod
    def _run_days(lift_days: list[int], templates: list[str], kinds: list[str]) -> list[int]:
        """Long run at the weekend, intervals mid-week away from the day
        before a leg day, easy runs on the free days left (else after an
        upper-body session)."""
        lower_next = {d - 1 for d, t in zip(lift_days, templates) if TEMPLATES[t][1]}
        upper = [d for d, t in zip(lift_days, templates) if not TEMPLATES[t][1]]
        free = [d for d in range(7) if d not in lift_days]
        used: list[int] = []

        def take(prefer: list[int], fallback: list[int]) -> int:
            for d in prefer + fallback:
                if d not in used:
                    used.append(d)
                    return d
            used.append(6)
            return 6

        days = {}
        if "long" in kinds:
            days["long"] = take([d for d in (6, 5) if d in free], [6])
        if "intervals" in kinds:
            mid = [d for d in (2, 3, 1, 4) if d in free]
            days["intervals"] = take([d for d in mid if d not in lower_next] + mid, [d for d in free] + upper + [2])
        out = []
        for kind in kinds:
            if kind == "easy":
                out.append(take([d for d in (1, 3, 4, 2, 5, 0) if d in free], upper + list(range(7))))
            else:
                out.append(days[kind])
        return out

    @staticmethod
    def _run_minutes(kind: str, kinds: list[str], weekly: float) -> int:
        """The week's running time split so the long run is the longest."""
        share = RUN_SHARES[len(kinds)][kind]
        floor = 30 if kind == "intervals" else 20
        return int(max(floor, round(weekly * share / 5) * 5))

    def run_base(self, block: int) -> int:
        """Weekly running minutes for a block: from what Strava shows he
        actually ran in the 4 weeks before, held within ±15% of the last
        block's base (more if he ran more, less if he didn't)."""
        p = self.profile()
        bases = p.setdefault("run_bases", {})
        if str(block) in bases:
            return bases[str(block)]
        runs = self.run_minutes_by_day(28)
        actual = sum(m for _, m in runs) / 4 if len(runs) >= 4 else 0  # too few runs to judge: ignore
        prev = bases.get(str(block - 1))
        if prev:
            base = min(prev * 1.15, max(prev * 0.9, actual * 1.05 if actual else prev))
        else:
            base = actual or DEFAULT_RUN_BASE[p["experience"]]
        base = int(max(20 * p["run_days"], round(base / 5) * 5))
        bases[str(block)] = base
        self._save_profile(p)
        return base

    def plan_week(self, mon: date) -> list[dict]:
        """This week's sessions as rows (created the first time they're
        needed; days before the program began are left out)."""
        if not self.profile():
            return []
        first = date.fromisoformat(self.profile()["first_day"])
        today = self.clock.today()
        end = mon + timedelta(days=6)
        rows = self.sessions(mon, end)
        have = {(r["day"], r["kind"], r["template"]) for r in rows}
        new = [s for s in self.desired_week(mon)
               if (s[0], s[1], s[2]) not in have and date.fromisoformat(s[0]) >= first
               and (not rows or date.fromisoformat(s[0]) >= today)]
        if new:
            with self.store._db() as c:
                c.executemany("insert into coach_sessions (day, kind, template, minutes) values (?, ?, ?, ?)", new)
            rows = self.sessions(mon, end)
        return rows

    def sessions(self, start: date, end: date) -> list[dict]:
        with self.store._db() as c:
            rows = c.execute("select * from coach_sessions where day between ? and ? order by day, kind, id",
                             (start.isoformat(), end.isoformat())).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["results"] = json.loads(d["results"] or "{}")
            out.append(d)
        return out

    def session(self, sid: int) -> dict | None:
        with self.store._db() as c:
            r = c.execute("select * from coach_sessions where id = ?", (sid,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["results"] = json.loads(d["results"] or "{}")
        return d

    def _update_session(self, sid: int, **fields) -> None:
        if "results" in fields:
            fields["results"] = json.dumps(fields["results"])
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.store._db() as c:
            c.execute(f"update coach_sessions set {sets} where id = ?", (*fields.values(), sid))

    def today(self) -> list[dict]:
        d = self.clock.today()
        self.plan_week(monday(d))
        return [s for s in self.sessions(d, d)]

    # -------- lifts

    def lift(self, key: str) -> dict:
        with self.store._db() as c:
            r = c.execute("select * from coach_lifts where exercise = ?", (key,)).fetchone()
        if r:
            d = dict(r)
            d["history"] = json.loads(d["history"] or "[]")
            return d
        return {"exercise": key, "load": None, "reps": LIB[key].lo, "misses": 0, "history": []}

    def _save_lift(self, s: dict) -> None:
        with self.store._db() as c:
            c.execute(
                "insert into coach_lifts (exercise, load, reps, misses, history, updated_at) values (?, ?, ?, ?, ?, ?) "
                "on conflict (exercise) do update set load = excluded.load, reps = excluded.reps, "
                "misses = excluded.misses, history = excluded.history, updated_at = excluded.updated_at",
                (s["exercise"], s["load"], s["reps"], s["misses"], json.dumps(s["history"][-30:]), to_iso(utc_now())),
            )

    def set_weight(self, key: str, kg: float) -> dict:
        ex = LIB[key]
        if ex.inc is None:
            raise ValueError(f"{ex.name} is bodyweight: it progresses by reps")
        if not 0 < float(kg) < 500:
            raise ValueError("weight must be in kg, between 0 and 500")
        s = self.lift(key)
        s["load"] = float(kg)
        self._save_lift(s)
        return s

    @staticmethod
    def progress(ex: Exercise, s: dict, outcome: str) -> dict:
        """Double progression. Returns the new state (not saved)."""
        s = {**s, "history": list(s.get("history", []))}
        load, reps = s["load"], s["reps"]
        loadable = ex.inc is not None and load is not None
        if outcome in ("hit", "easy"):
            s["misses"] = 0
            step = 2 if outcome == "easy" else 1
            if reps >= ex.hi and loadable:  # top of the range: heavier, back to the bottom
                s["load"], s["reps"] = _round_to(load + ex.inc, ex.inc), ex.lo
            else:
                s["reps"] = min(ex.hi, reps + step)
        elif outcome == "missed":
            s["misses"] = s.get("misses", 0) + 1
            if s["misses"] >= 2:
                if loadable:
                    s["load"] = max(ex.inc, _round_to(load * 0.9, ex.inc))
                    s["reps"] = ex.lo
                else:
                    s["reps"] = max(ex.lo, reps - 2)
                s["misses"] = 0
        return s

    # -------- what a session asks for

    def lift_plan(self, template: str, day: date, readiness: str = "green") -> list[dict]:
        p = self.profile()
        _, wk = self.block_week(day)
        eq = EQUIPMENT.index(p["equipment"])
        cap = EXERCISES_BY_MINUTES[p["minutes"]]
        out = []
        for i, (slot, sets) in enumerate(TEMPLATES[template][2]):
            key = SLOTS[slot][eq]
            if key is None or any(x["key"] == key for x in out):
                continue
            ex = LIB[key]
            st = self.lift(key)
            if wk == 3:  # deload: ~60% of the sets, 10% lighter, easy
                sets, load = max(1, round(sets * 0.6)), st["load"]
                load = _round_to(load * 0.9, ex.inc) if (load and ex.inc) else load
            else:
                load = st["load"]
                if wk == 2 and len(out) < 2 and p["experience"] != "new":
                    sets += 1  # peak week: one more set on the two main lifts
            if readiness == "red":
                sets = max(1, sets - 1)
            rir = WEEK_RIR[wk] + (1 if readiness == "red" else 0)
            out.append({"key": key, "name": ex.name, "short": ex.short, "sets": sets, "reps": st["reps"],
                        "load": load, "bodyweight": ex.inc is None, "rir": rir, "deload": wk == 3})
            if len(out) >= cap:
                break
        return out

    def run_plan(self, s: dict, readiness: str = "green") -> str:
        kind, mins = s["template"], s["minutes"] or 30
        _, wk = self.block_week(date.fromisoformat(s["day"]))
        typical = self.typical_pace()
        easy = f"{_pace(typical + 30)}/km or slower" if typical else "conversational pace"
        hr = self._z2_cap()
        easy += f", HR under {hr}" if hr else ""
        if kind == "intervals" and readiness in ("amber", "red"):
            return f"Easy run {int(mins * 0.75)} min ({easy}): intervals swapped out, readiness is {readiness}"
        if kind == "long" and readiness == "red":
            return f"Long run cut to {int(mins * 0.75)} min, all easy ({easy}): readiness is red"
        if kind == "intervals":
            hard = f" (hard ≈ {_pace(typical - 45)}/km)" if typical else " (hard: RPE 8, breathing hard)"
            return f"Intervals {mins} min: 10 min easy, {INTERVALS[wk]}{hard}, easy to finish"
        if kind == "long":
            return f"Long run {mins} min, easy ({easy})"
        return f"Easy run {mins} min ({easy})"

    def typical_pace(self) -> float | None:
        """Median pace (s/km) of his runs of 15+ minutes in the last 6 weeks."""
        paces = []
        for log in self.store.get_logs(None, "workout", 42, limit=200):
            note = log.get("note") or ""
            m = re.search(r"(\d+):(\d\d)/km", note)
            if m and note.startswith(RUN_SPORTS) and (log["value"] or 0) >= 15:
                paces.append(int(m.group(1)) * 60 + int(m.group(2)))
        return statistics.median(paces) if len(paces) >= 2 else None

    def _z2_cap(self) -> int | None:
        age = (self.profile() or {}).get("age")
        return round((208 - 0.7 * age) * 0.75) if age else None

    # -------- load and readiness

    def run_minutes_by_day(self, days: int) -> list[tuple[str, float]]:
        out = []
        for log in self.store.get_logs(None, "workout", days, limit=500):
            if (log.get("note") or "").startswith(RUN_SPORTS):
                out.append((self.clock.local(log["logged_at"]).date().isoformat(), log["value"] or 0))
        return out

    def training_minutes(self, days: int) -> float:
        """Workout minutes (Strava and others) plus finished coach lift
        sessions on days Strava didn't already record a strength session."""
        logs = self.store.get_logs(None, "workout", days, limit=1000)
        total = sum(l["value"] or 0 for l in logs)
        strava_strength = {self.clock.local(l["logged_at"]).date().isoformat() for l in logs
                           if (l.get("note") or "").startswith(("Strength", "Workout", "CrossFit", "HIIT"))}
        start = self.clock.today() - timedelta(days=days - 1)
        for s in self.sessions(start, self.clock.today()):
            if s["kind"] == "lift" and s["status"] == "done" and s["day"] not in strava_strength:
                total += s["minutes"] or 0
        return total

    def load_ratio(self) -> float | None:
        """Acute:chronic workload: last 7 days against the 28-day weekly average."""
        acute, chronic = self.training_minutes(7), self.training_minutes(28) / 4
        return round(acute / chronic, 2) if chronic >= 30 else None

    def _recent(self, metric: str, hours: int = 20) -> float | None:
        """The latest value logged in the last `hours` (last night's sleep is
        logged the evening before or that morning)."""
        rows = self.store.get_logs(None, metric, 2, limit=1)
        if rows and rows[0]["value"] is not None and (utc_now() - _parse(rows[0]["logged_at"])).total_seconds() < hours * 3600:
            return rows[0]["value"]
        return None

    def readiness(self) -> tuple[str, list[str]]:
        reasons, level = [], "green"
        sleep = self._recent("sleep_hours")
        mood = self._recent("mood")
        ratio = self.load_ratio()
        if sleep is not None:
            reasons.append(f"sleep {sleep:g}h")
            level = "red" if sleep < 5.5 else "amber" if sleep < 6.5 else level
        if mood is not None:
            reasons.append(f"mood {mood:g}/5")
            if mood <= 2:
                level = "red"
        if ratio is not None:
            reasons.append(f"load {ratio:g}×")
            if ratio > 1.5:
                level = "red"
            elif ratio > 1.3 and level == "green":
                level = "amber"
        return level, reasons

    # -------- tracking

    def record(self, sid: int, key: str, outcome: str) -> dict:
        s = self.session(sid)
        if s is None or s["kind"] != "lift":
            raise ValueError("no such lifting session")
        if key not in LIB:
            raise ValueError(f"unknown exercise {key}")
        if outcome not in MARK:
            raise ValueError("outcome must be hit, easy or missed")
        s["results"][key] = outcome
        self._update_session(sid, results=s["results"])
        return s

    def finish(self, sid: int) -> str:
        """Apply the session's results to each lift (not in a deload week)
        and mark it done. Returns what changes for next time."""
        s = self.session(sid)
        if s is None or s["kind"] != "lift":
            raise ValueError("no such lifting session")
        if s["status"] == "done":
            return "Already finished."
        day = date.fromisoformat(s["day"])
        _, wk = self.block_week(day)
        changes = []
        for key, outcome in s["results"].items():
            ex, st = LIB[key], self.lift(key)
            st["history"].append({"day": s["day"], "load": st["load"], "reps": st["reps"], "outcome": outcome})
            new = st if wk == 3 else self.progress(ex, st, outcome)
            self._save_lift(new)
            if (new["load"], new["reps"]) != (st["load"], st["reps"]):
                changes.append(f"{ex.short} {self._target(ex, new)}")
        self._update_session(sid, status="done", completed_at=to_iso(utc_now()))
        done = len(s["results"])
        self.store.add_log_once(f"coach:{sid}", self.area, "strength", s["minutes"], "min",
                                f"{TEMPLATES[s['template']][0]}: {done} exercises logged", None)
        if wk == 3:
            return f"Deload session done ({done} exercises). Loads stay put this week."
        return "Done. Next time: " + ("; ".join(changes) if changes else "same targets, own them.")

    @staticmethod
    def _target(ex: Exercise, st: dict) -> str:
        if ex.inc is None:
            return f"{st['reps']} reps"
        return f"{st['load']:g} kg × {st['reps']}" if st["load"] is not None else f"× {st['reps']}"

    def complete_run(self, sid: int, status: str = "done") -> None:
        self._update_session(sid, status=status, completed_at=to_iso(utc_now()))

    def autocomplete_runs(self) -> int:
        """Planned runs that Strava shows he did (a run that day) are ticked off."""
        today = self.clock.today()
        runs = {}
        for day, mins in self.run_minutes_by_day(15):
            runs[day] = runs.get(day, 0) + mins
        n = 0
        for s in self.sessions(today - timedelta(days=14), today):
            if s["kind"] == "run" and s["status"] == "planned" and s["day"] in runs:
                self._update_session(s["id"], status="done", completed_at=to_iso(utc_now()),
                                     results={"strava_minutes": round(runs[s["day"]])})
                n += 1
        return n

    # -------- review: compliance, progress, load, physique loop

    def weight_trend(self) -> tuple[float, float] | None:
        """(%/week change, latest 7-day average): last 7 days' average
        weight against the 7 days two weeks before. Needs 2+ weigh-ins in each."""
        logs = self.store.get_logs(None, "weight", 21, limit=200)
        now = utc_now()

        def avg(lo, hi):
            v = [l["value"] for l in logs if l["value"]
                 and lo <= (now - _parse(l["logged_at"])).days < hi]
            return sum(v) / len(v) if len(v) >= 2 else None

        recent, before = avg(0, 7), avg(14, 21)
        if recent is None or before is None:
            return None
        return round((recent - before) / before * 100 / 2, 2), round(recent, 1)

    def review(self, apply: bool = True) -> str:
        p = self.profile()
        if not p:
            return "No program yet: set one up first."
        self.autocomplete_runs()
        today = self.clock.today()
        start = today - timedelta(days=6)
        week = self.sessions(start, today)
        due = [s for s in week if s["day"] <= today.isoformat()]
        done = [s for s in due if s["status"] == "done"]
        lines = [f"Last 7 days: {len(done)} of {len(due)} planned sessions done"
                 + (f" ({round(100 * len(done) / len(due))}%)." if due else ".")]
        missed = [f"{s['day'][5:]} {self._title(s)}" for s in due if s["status"] != "done" and s["day"] < today.isoformat()]
        if missed:
            lines.append("Missed: " + ", ".join(missed) + ".")
        ups = []
        with self.store._db() as c:
            rows = c.execute("select * from coach_lifts").fetchall()
        for r in rows:
            hist = [h for h in json.loads(r["history"] or "[]") if h["day"] >= start.isoformat()]
            if hist:
                ex = LIB.get(r["exercise"])
                first = hist[0]
                if ex and (r["load"] or 0, r["reps"]) > (first["load"] or 0, first["reps"]):
                    ups.append(f"{ex.short} {self._target(ex, {'load': first['load'], 'reps': first['reps']})} → "
                               f"{self._target(ex, dict(r))}")
        if ups:
            lines.append("Progressed: " + "; ".join(ups) + ".")
        planned_run = sum(s["minutes"] or 0 for s in due if s["kind"] == "run")
        ran = sum(m for d, m in self.run_minutes_by_day(7))
        if planned_run or ran:
            lines.append(f"Running: {ran:.0f} of {planned_run} planned minutes.")
        ratio = self.load_ratio()
        if ratio is not None:
            word = "spiking, ease off" if ratio > 1.5 else "high" if ratio > 1.3 else "low, room to build" if ratio < 0.8 else "in the sweet spot"
            lines.append(f"Load ratio {ratio:g} ({word}).")
        lines.append(self._physique_loop(p, apply))
        blk, wk = self.block_week(today + timedelta(days=1))
        lines.append(f"Next week: block {blk + 1}, week {wk + 1} ({WEEK_NAME[wk]}).")
        return "\n".join(l for l in lines if l)

    def _physique_loop(self, p: dict, apply: bool) -> str:
        """Bodyweight trend against the goal's target rate; out of range
        two reviews running moves the calorie adjustment by 150 (±600 max)."""
        trend = self.weight_trend()
        label, _, (lo, hi) = GOALS[p["goal"]]
        if trend is None:
            return "Weight trend: not enough weigh-ins (log your weight 3+ times a week, same time of day)."
        rate, avg = trend
        verdict = "on target" if lo <= rate <= hi else "below target" if rate < lo else "above target"
        text = f"Weight {avg} kg, {rate:+.2f}%/week; {label} wants {lo:+g} to {hi:+g}%: {verdict}."
        week = monday(self.clock.today()).isoformat()
        if apply and p.get("reviewed_week") != week:  # counts once a week, however often it's asked
            p["reviewed_week"] = week
            streak = p.get("off_target", 0) + 1 if verdict != "on target" else 0
            p["off_target"] = streak
            if streak >= 2:
                step = 150 if rate < lo else -150
                p["kcal_adjust"] = max(-600, min(600, p.get("kcal_adjust", 0) + step))
                p["off_target"] = 0
                text += f" Calories {'+' if step > 0 else ''}{step}/day from now (adjustment {p['kcal_adjust']:+d})."
            self._save_profile(p)
        return text

    @staticmethod
    def _title(s: dict) -> str:
        return TEMPLATES[s["template"]][0] if s["kind"] == "lift" else RUN_TITLE.get(s["template"], s["template"])

    # -------- text

    def today_text(self) -> str:
        if not self.profile():
            return "No training program yet: set one up (coach_setup, or 🏋 Train on Telegram)."
        level, reasons = self.readiness()
        blk, wk = self.block_week(self.clock.today())
        head = f"Block {blk + 1} week {wk + 1} ({WEEK_NAME[wk]}) · readiness {level}" + (
            f" ({', '.join(reasons)})" if reasons else "")
        sessions = self.today()
        if not sessions:
            nxt = self.next_session()
            return head + "\nRest day." + (f" Next: {nxt}." if nxt else "")
        return head + "\n" + "\n".join(self.session_text(s, level) for s in sessions)

    def session_text(self, s: dict, level: str = "green") -> str:
        status = " ✅" if s["status"] == "done" else " (skipped)" if s["status"] == "skipped" else ""
        if s["kind"] == "run":
            return self.run_plan(s, level) + status
        lines = [f"{TEMPLATES[s['template']][0]} ({s['minutes']} min){status}"]
        for i, x in enumerate(self.lift_plan(s["template"], date.fromisoformat(s["day"]), level), 1):
            mark = MARK.get(s["results"].get(x["key"]), "")
            lines.append(f"{mark + ' ' if mark else ''}{i}. {x['name']} {self._prescription(x)}")
        return "\n".join(lines)

    @staticmethod
    def _prescription(x: dict) -> str:
        sets = f"{x['sets']}×{x['reps']}"
        rir = f"{x['rir']} in reserve"
        if x["bodyweight"]:
            return f"{sets} bodyweight · {rir}"
        if x["load"] is None:
            return f"{sets} · pick a weight with {x['rir']} reps in reserve, then ⚖ it"
        return f"{sets} @ {x['load']:g} kg · {rir}" + (" (deload)" if x["deload"] else "")

    def next_session(self) -> str | None:
        today = self.clock.today()
        for offset in range(1, 8):
            d = today + timedelta(days=offset)
            self.plan_week(monday(d))
            ss = self.sessions(d, d)
            if ss:
                return f"{d:%a}: " + " + ".join(self._title(s) for s in ss)
        return None

    def week_text(self, offset: int = 0) -> str:
        mon = monday(self.clock.today()) + timedelta(days=7 * offset)
        rows = self.plan_week(mon)
        blk, wk = self.block_week(mon)
        lines = [f"Week of {mon:%d %b}: block {blk + 1}, week {wk + 1} ({WEEK_NAME[wk]})"]
        for d in range(7):
            day = mon + timedelta(days=d)
            ss = [s for s in rows if s["day"] == day.isoformat()]
            if not ss and offset == 0 and day < date.fromisoformat(self.profile()["first_day"]):
                continue
            items = []
            for s in ss:
                mark = "✅ " if s["status"] == "done" else "⏭ " if s["status"] == "skipped" else ""
                mins = f" {s['minutes']}′" if s["kind"] == "run" else ""
                items.append(f"{mark}{self._title(s)}{mins}")
            lines.append(f"{day:%a}: " + (" + ".join(items) if items else "rest"))
        return "\n".join(lines)


def _parse(iso: str):
    from .clock import from_iso

    return from_iso(iso)


# ---------------------------------------------------------------- Telegram views


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data[:64]}


SETUP_STEPS = [
    ("goal", "What's the main goal?", [(v[0], k) for k, v in GOALS.items()]),
    ("lift_days", "Lifting days a week?", [(str(n), str(n)) for n in (2, 3, 4, 5, 6)]),
    ("run_days", "Runs a week?", [(str(n), str(n)) for n in (0, 1, 2, 3, 4)]),
    ("equipment", "What do you train with?", [("Full gym", "gym"), ("Dumbbells", "dumbbells"), ("Bodyweight", "bodyweight")]),
    ("experience", "Lifting experience?", [(v, k) for k, v in EXPERIENCE.items()]),
    ("minutes", "How long per lifting session?", [("45 min", "45"), ("60 min", "60"), ("75 min", "75")]),
]


def setup_step(i: int) -> tuple[str, dict]:
    field, question, options = SETUP_STEPS[i]
    rows = [[_btn(label, f"co:{i}:{value}") for label, value in options[j:j + 3]] for j in range(0, len(options), 3)]
    return f"🏋 Program setup {i + 1}/{len(SETUP_STEPS)}\n{question}", {"inline_keyboard": rows}


def today_card(coach: Coach) -> tuple[str, dict]:
    if not coach.profile():
        text, kb = setup_step(0)
        return "No program yet. Six taps and you have one.\n\n" + text, kb
    text = "🏋 " + coach.today_text()
    level, _ = coach.readiness()
    rows = []
    for s in coach.today():
        if s["kind"] == "lift" and s["status"] == "planned":
            for x in coach.lift_plan(s["template"], date.fromisoformat(s["day"]), level):
                key, sid = x["key"], s["id"]
                row = [_btn(f"✓ {x['short']}", f"cx:{sid}:{key}:h"), _btn("⤴", f"cx:{sid}:{key}:e"),
                       _btn("↓", f"cx:{sid}:{key}:m")]
                if not x["bodyweight"]:
                    row.append(_btn("⚖", f"cw:{sid}:{key}"))
                rows.append(row)
            rows.append([_btn("✅ Finish lifting", f"cf:{s['id']}")])
        elif s["kind"] == "run" and s["status"] == "planned":
            rows.append([_btn(f"✅ {RUN_TITLE[s['template']]} done", f"cr:{s['id']}:done"),
                         _btn("⏭ Skip", f"cr:{s['id']}:skipped")])
    rows.append([_btn("📅 Week", "cv:0"), _btn("Next week", "cv:1"), _btn("⚙ Program", "co:0:")])
    return text, {"inline_keyboard": rows}


def week_card(coach: Coach, offset: int) -> tuple[str, dict]:
    if not coach.profile():
        return today_card(coach)
    other = 1 if offset == 0 else 0
    return "📅 " + coach.week_text(offset), {"inline_keyboard": [[
        _btn("🏋 Today", "ct"), _btn("Next week" if other else "This week", f"cv:{other}")]]}


# ---------------------------------------------------------------- tools


def build_coach_tools(coach: Coach | None) -> list[Tool]:
    if coach is None:
        return []

    def today_keys() -> list[str]:
        level, _ = coach.readiness()
        keys = []
        for s in coach.today():
            if s["kind"] == "lift":
                keys += [x["key"] for x in coach.lift_plan(s["template"], date.fromisoformat(s["day"]), level)]
        return keys

    def find_key(name: str, prefer: list[str] = ()) -> str:
        """Today's exercises first ("bench" is the dumbbell bench on a
        dumbbell program), then the whole library."""
        n = " ".join(str(name).strip().lower().split())
        names = {n, n.rstrip("s"), n.replace("es ", " ").rstrip("s")}  # "rows", "bench presses"
        for key in prefer:
            ex = LIB[key]
            if names & {key, ex.name.lower(), ex.short.lower()} or any(m in ex.name.lower() for m in names) \
                    or ex.short.lower() in n:
                return key
        for key, ex in LIB.items():
            if names & {key, ex.name.lower(), ex.short.lower()}:
                return key
        for key, ex in LIB.items():
            if any(m in ex.name.lower() for m in names):
                return key
        raise ValueError(f"unknown exercise {name!r}; known: {', '.join(e.name for e in LIB.values())}")

    def log(exercise: str, outcome: str | None = None, weight_kg: float | None = None) -> str:
        key = find_key(exercise, today_keys())
        out = []
        if weight_kg:
            coach.set_weight(key, weight_kg)
            out.append(f"{LIB[key].name}: working weight {weight_kg:g} kg.")
        if outcome:
            lift = [s for s in coach.today() if s["kind"] == "lift" and s["status"] == "planned"]
            if not lift:
                out.append("No open lifting session today, so no outcome logged.")
            elif key not in today_keys():
                title = TEMPLATES[lift[0]["template"]][0]
                names = ", ".join(LIB[k].name for k in today_keys())
                out.append(f"{LIB[key].name} isn't in today's session ({title}: {names}), so no outcome logged.")
            else:
                coach.record(lift[0]["id"], key, outcome)
                out.append(f"{LIB[key].name}: {outcome} (applies when the session is finished).")
        return " ".join(out) or "Nothing to log: give an outcome or a weight."

    def finish() -> str:
        lift = [s for s in coach.today() if s["kind"] == "lift" and s["status"] == "planned"]
        if not lift:
            return "No open lifting session today."
        return coach.finish(lift[0]["id"])

    return [
        Tool(
            name="coach_setup",
            description=("Set up or change his training program (lifting split + runs, 4-week blocks, automatic "
                         "progression). Changing lifting or running days starts a new block; other changes keep it."),
            parameters={"type": "object", "properties": {
                "goal": {"type": "string", "enum": list(GOALS), "description": "muscle, lean (fat loss), recomp, run"},
                "lift_days": {"type": "integer", "description": "0-6"},
                "run_days": {"type": "integer", "description": "0-4"},
                "equipment": {"type": "string", "enum": list(EQUIPMENT)},
                "experience": {"type": "string", "enum": list(EXPERIENCE), "description": "new, mid (1-3 y), adv (3+ y)"},
                "minutes": {"type": "integer", "enum": [45, 60, 75]},
                "age": {"type": "integer", "description": "optional: sets heart-rate guidance"},
            }, "required": []},
            func=lambda **kw: coach.set_option(**kw) if coach.profile() else coach.setup(**{
                "goal": "recomp", "lift_days": 3, "run_days": 2, "equipment": "gym", "experience": "mid", **kw}),
        ),
        Tool(
            name="coach_today",
            description=("Today's prescribed workout: each lift with sets × reps @ weight and reps in reserve, runs "
                         "with time and pace, adjusted for readiness (sleep, mood, training-load ratio)."),
            parameters={"type": "object", "properties": {}},
            func=coach.today_text,
        ),
        Tool(
            name="coach_week",
            description="The training week: which session falls on which day, and what's done. offset 1 = next week.",
            parameters={"type": "object", "properties": {"offset": {"type": "integer"}}},
            func=lambda offset=0: coach.week_text(int(offset or 0)),
        ),
        Tool(
            name="coach_log",
            description=("Log a lift from today's session: outcome hit (all sets at target reps), easy (target met "
                         "with reps to spare) or missed; and/or his working weight in kg. Use it when he texts "
                         "results like \"bench 80 kg, all sets\"; then coach_finish when the session is over."),
            parameters={"type": "object", "properties": {
                "exercise": {"type": "string", "description": "name, e.g. 'bench press'"},
                "outcome": {"type": "string", "enum": ["hit", "easy", "missed"]},
                "weight_kg": {"type": "number"},
            }, "required": ["exercise"]},
            func=log,
        ),
        Tool(
            name="coach_finish",
            description="Finish today's lifting session: applies progression to each logged lift and says what changes.",
            parameters={"type": "object", "properties": {}},
            func=finish,
        ),
        Tool(
            name="coach_review",
            description=("Weekly training review: sessions done vs planned, lifts that progressed, running minutes, "
                         "load ratio, and the bodyweight trend against his goal (which moves the calorie adjustment "
                         "used by fuel_targets). Use in the weekly review."),
            parameters={"type": "object", "properties": {}},
            func=coach.review,
        ),
    ]
