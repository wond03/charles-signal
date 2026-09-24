#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断：真实 OKX demo bills(type=2) 的 subType/side 分布，验证方向判定。
只读，不下单。输出到 stdout 供 Actions 日志查看。
"""
import json
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")
import okx_exec

BJ = timezone(timedelta(hours=8))


def main():
    end_dt = datetime.now(BJ)
    start_dt = end_dt - timedelta(hours=48)
    end_ms = int(end_dt.timestamp() * 1000)
    start_ms = int(start_dt.timestamp() * 1000)
    rows = []
    for inst in ("BTC-USDT-SWAP", "XAU-USDT-SWAP"):
        after = ""
        for _ in range(6):
            params = {"instType": "SWAP", "instId": inst,
                      "begin": str(start_ms), "end": str(end_ms),
                      "type": "2", "limit": "100"}
            if after:
                params["after"] = after
            data = okx_exec._private_request("GET", "/api/v5/account/bills", params=params)
            batch = data.get("data", [])
            rows.extend(batch)
            if len(batch) < 100:
                break
            after = batch[-1].get("billId", "")
            if not after:
                break
    print(f"[probe] bills type=2 总数: {len(rows)} 窗口: {start_dt:%m-%d %H:%M} ~ {end_dt:%m-%d %H:%M}")
    # subType 分布
    dist = {}
    for r in rows:
        sub = str(r.get("subType") or "?")
        side = str(r.get("side") or "?")
        pnl = float(r.get("pnl") or 0.0)
        fee = float(r.get("fee") or 0.0)
        d = dist.setdefault(sub, {"n": 0, "pnl": 0.0, "fee": 0.0, "sides": {}})
        d["n"] += 1
        d["pnl"] += pnl
        d["fee"] += fee
        d["sides"][side] = d["sides"].get(side, 0) + 1
    print("[probe] subType 分布 (n / pnl_sum / fee_sum / sides):")
    for sub in sorted(dist, key=lambda x: -dist[x]["n"]):
        d = dist[sub]
        print(f"  subType={sub} n={d['n']} pnl={d['pnl']:+.4f} fee={d['fee']:+.4f} sides={d['sides']}")
    # 样本 40 条
    print("[probe] 样本(instId/subType/side/pnl/fee/sz/ts):")
    for r in rows[:40]:
        print(f"  {r.get('instId')} sub={r.get('subType')} side={r.get('side')} "
              f"pnl={r.get('pnl')} fee={r.get('fee')} sz={r.get('sz')} ts={r.get('ts')} "
              f"ordId={r.get('ordId')} posBalChg={r.get('posBalChg')} balChg={r.get('balChg')}")
    # 平仓候选（pnl!=0 或 subType in 5/6/200+）统计
    closed = [r for r in rows if float(r.get("pnl") or 0.0) != 0.0 or str(r.get("subType")) in ("5", "6")]
    print(f"[probe] pnl!=0 或 subType 5/6 的记录数: {len(closed)}")
    sub_c = {}
    for r in closed:
        sub = str(r.get("subType") or "?")
        sub_c[sub] = sub_c.get(sub, 0) + 1
    print(f"[probe] 其中 subType 分布: {sub_c}")


if __name__ == "__main__":
    main()
