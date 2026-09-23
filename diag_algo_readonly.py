#!/usr/bin/env python3
"""只读诊断：查询 OKX Demo 当前 pending OCO 策略单与持仓，不进行任何撤单/下单操作。"""
import json
import sys

import okx_exec

print("=== 1) pending OCO 策略委托 ===")
try:
    pend = okx_exec.get_algo_orders(ord_type="oco")
    print(f"count: {len(pend)}")
    for a in pend[:50]:
        pick = {k: a.get(k) for k in [
            "instId", "algoId", "state", "sz", "ordType", "side", "posSide",
            "tpTriggerPx", "tpOrdPx", "slTriggerPx", "slOrdPx", "triggerPx", "actualPx",
            "cTime", "uTime",
        ]}
        print(json.dumps(pick, ensure_ascii=False))
except Exception as e:  # noqa: BLE001
    print("ERR", repr(e))

print("\n=== 2) 当前持仓 ===")
try:
    pos = okx_exec.get_positions()
    print(f"count: {len(pos)}")
    for p in pos[:30]:
        pick = {k: p.get(k) for k in [
            "instId", "posSide", "pos", "avgPx", "upl", "uplRatio", "lever",
            "liqPx", "margin", "mgnMode", "adl", "uTime", "closeOrderAlgo",
        ]}
        print(json.dumps(pick, ensure_ascii=False))
except Exception as e:  # noqa: BLE001
    print("ERR", repr(e))

print("\n=== 3) 余额 ===")
try:
    print("avail USDT:", okx_exec.get_balance())
except Exception as e:  # noqa: BLE001
    print("ERR", repr(e))
