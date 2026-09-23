#!/usr/bin/env python3
"""一次性修复：给 XAU short 当前持仓重新挂 OCO 止盈止损（SL/TP 与原计划一致）。"""
import json
import time

import okx_exec


def main():
    inst = "XAU_USDT"
    sl = 4308.31
    tp = 4263.58
    print("=== 1) 查当前 XAU 持仓 ===")
    pos = okx_exec.get_positions(inst)
    target = None
    for p in pos:
        if p.get("posSide") == "short":
            target = p
            break
    if target is None:
        print("无 XAU short 持仓，无需挂单")
        return
    qty = float(target.get("pos", 0))
    print(f"XAU short 持仓 {qty} 张 avgPx={target.get('avgPx')} liqPx={target.get('liqPx')}")
    if qty <= 0:
        print("持仓为 0，无需挂单")
        return
    print(f"=== 2) 挂 OCO: SL={sl} TP={tp} qty={qty} ===")
    algo = okx_exec.place_algo_sltp(inst, "short", qty, sl, tp)
    print("挂单返回:", json.dumps(algo, ensure_ascii=False))
    algo_id = algo.get("algoId", "")
    if not algo_id:
        print("挂单失败，无 algoId")
        return
    print("=== 3) 校验 pending ===")
    time.sleep(2)
    pend = okx_exec.get_algo_orders(inst=inst, ord_type="oco")
    found = [a for a in pend if str(a.get("algoId")) == str(algo_id)]
    print(f"pending 校验: 找到 {len(found)} 条")
    for a in found:
        print(json.dumps({k: a.get(k) for k in ["algoId", "state", "sz", "tpTriggerPx", "slTriggerPx", "posSide"]},
                         ensure_ascii=False))
    print("done")


if __name__ == "__main__":
    main()
