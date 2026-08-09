#!/usr/bin/env python3
"""PARITY — the browser tool and the Python research simulator implement the SAME ladder.
If they disagree numerically, one of them is wrong. This is the only test that can catch
a rule drifting between the thing that was validated and the thing being traded.

Emits a JSON case list for parity.js to replay through the real page.
"""
import json, subprocess, sys, os
sys.path.insert(0, os.path.expanduser("~/riskml"))
from riskml.sim.ladder import Ladder, initial_stop

# Take the parameters from Ladder() rather than restating them: this comment used to read
# "3x ATR init, 2.5x trail, [8,45] clamp" long after riskml had moved to 2.5 / 3.5 / [8,30],
# and a stale comment next to a parity check is worse than none — it is the thing someone
# reads when deciding whether a mismatch is real. riskml/sim/ladder.py is the source of truth.
P = Ladder()          # 45d horizon; every numeric parameter is emitted below and compared
CASES = [(100, 1.0), (100, 2.0), (100, 3.25), (100, 5.0), (100, 8.0), (100, 15.0),
         (338.19, 8.02), (16.18, 1.71), (53.03, 6.48), (4.5, 0.4), (1200, 30.0)]

out = []
for price, atr in CASES:
    atr_pct = atr / price * 100.0
    arm_trig = max(P.arm_floor_pct, P.arm_atr_mult * atr_pct) if P.arm_atr_mult > 0 else P.arm_pct
    out.append({"price": price, "atr": atr,
                "py_stop": round(initial_stop(price, atr, P), 6),
                "py_arm_trigger_pct": round(arm_trig, 4)})

# ---- arming reference: a spike that TOUCHES the trigger but CLOSES below it -----------
# This is the parabolic give-back case. If the two implementations disagree on whether it
# arms, the tool protects a real position differently from the rule that was validated.
from riskml.sim.ladder import simulate
import numpy as np
def spike_case(price, atr):
    trig = max(P.arm_floor_pct, P.arm_atr_mult * (atr / price * 100.0))
    hi = price * (1 + (trig + 1.0) / 100.0)      # high clears the trigger by 1pp
    cl = price * (1 + (trig - 5.0) / 100.0)      # close falls 5pp short of it
    # day 1: the spike. days 2-5: quiet drift, never near the stop.
    fwd = [[price, hi, price * 0.99, cl]]
    for _ in range(4):
        fwd.append([cl, cl * 1.005, cl * 0.99, cl])
    o = simulate(price, atr, np.array(fwd, dtype=float), P)
    return {"price": price, "atr": atr, "spike_high": round(hi, 6),
            "spike_close": round(cl, 6), "arm_trigger_pct": round(trig, 4),
            "py_armed": bool(o.armed), "py_stop_at_end": round(o.exit_price, 6)
            if o.exit_idx >= 0 else None}
SPIKES = [spike_case(p, a) for p, a in CASES[:4]]

print(json.dumps({"params": {"init": P.init_atr_mult, "trail": P.trail_atr_mult,
                             "arm": P.arm_floor_pct, "armatrmult": P.arm_atr_mult,
                             "arm_on_close": P.arm_on_close,
                             "min": P.min_stop_pct, "max": P.max_stop_pct},
                  "cases": out, "spikes": SPIKES}))
