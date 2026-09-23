#!/usr/bin/env python3
"""只读诊断 v2：查询 OKX Demo XAU 的 algo 历史与订单历史，定位止盈止损为何不在。只读不操作。"""
import json

import okx_exec

print("=== 1) XAU OCO algo 历史(orders-algo-history, state=live) ===")
try:
    d = okx_exec._private_request("GET", "/api/v5/trade/orders-algo-history",
                                  params={"instId": "XAU-USDT-SWAP", "ordType": "oco", "state": "live"})
    rows = d.get("data", [])
    print("count:", len(rows))
    for a in rows[:50]:
        print(json.dumps({k: a.get(k) for k in ["instId", "algoId", "ordType", "side", "posSide", "sz",
                                                 "tpTriggerPx", "slTriggerPx", "state", "cTime", "uTime"]},
                         ensure_ascii=False))
except Exception as e:  # noqa: BLE001
    print("ERR", repr(e))

print("\n=== 2) XAU 订单历史(近50条, 任意状态) ===")
try:
    d2 = okx_exec._private_request("GET", "/api/v5/trade/orders-history",
                                   params={"instId": "XAU-USDT-SWAP", "ordType": "market", "state": "filled", "limit": "50"})
    rows2 = d2.get("data", [])
    print("count:", len(rows2))
    for o in rows2[:50]:
        print(json.dumps({k: o.get(k) for k in ["instId", "ordId", "clOrdId", "side", "posSide", "sz", "avgPx",
                                                 "state", "fee", "pnl", "cTime", "uTime", "fillPx", "accFillSz"]},
                         ensure_ascii=False))
except Exception as e:  # noqa: BLE001
    print("ERR", repr(e))
