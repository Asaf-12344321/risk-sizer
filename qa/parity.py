#!/usr/bin/env python3
"""PARITY — the browser tool and the Python research simulator implement the SAME ladder.
If they disagree numerically, one of them is wrong. This is the only test that can catch
a rule drifting between the thing that was validated and the thing being traded.

Emits a JSON case list for parity.js to replay through the real page.
"""
import json, subprocess, sys, os
sys.path.insert(0, os.path.expanduser("~/riskml"))
from riskml.sim.ladder import Ladder, initial_stop

P = Ladder()          # 45d, 3x ATR init, 2.5x trail, +15% arm, [8,45] clamp in riskml
CASES = [(100, 1.0), (100, 2.0), (100, 3.25), (100, 5.0), (100, 8.0), (100, 15.0),
         (338.19, 8.02), (16.18, 1.71), (53.03, 6.48), (4.5, 0.4), (1200, 30.0)]

out = []
for price, atr in CASES:
    atr_pct = atr / price * 100.0
    arm_trig = max(P.arm_floor_pct, P.arm_atr_mult * atr_pct) if P.arm_atr_mult > 0 else P.arm_pct
    out.append({"price": price, "atr": atr,
                "py_stop": round(initial_stop(price, atr, P), 6),
                "py_arm_trigger_pct": round(arm_trig, 4)})
print(json.dumps({"params": {"init": P.init_atr_mult, "trail": P.trail_atr_mult,
                             "arm": P.arm_floor_pct, "armatrmult": P.arm_atr_mult,
                             "min": P.min_stop_pct, "max": P.max_stop_pct},
                  "cases": out}))
