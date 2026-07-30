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
    out.append({"price": price, "atr": atr,
                "py_stop": round(initial_stop(price, atr, P), 6),
                "py_init_mult": P.init_atr_mult,
                "py_min": P.min_stop_pct, "py_max": P.max_stop_pct,
                "py_arm": P.arm_pct, "py_trail_mult": P.trail_atr_mult})
print(json.dumps({"params": {"init": P.init_atr_mult, "trail": P.trail_atr_mult,
                             "arm": P.arm_pct, "min": P.min_stop_pct, "max": P.max_stop_pct},
                  "cases": out}))
