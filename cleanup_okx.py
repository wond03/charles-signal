#!/usr/bin/env python3
"""一次性清理：撤销全部 OCO 挂单 + 市价平掉全部持仓（修复前算错的单）"""
import sys
import time

import okx_exec


def cancel_all_oco():
    pend = okx_exec.get_algo_orders(ord_type="oco")
    total = len(pend)
    ok = 0
    for a in pend:
        inst_okx = a.get("instId", "")
        inst = "BTC_USDT" if "BTC" in inst_okx else "XAU_USDT"
        algo_id = a.get("algoId", "")
        for attempt in range(3):
            try:
                r = okx_exec.cancel_algo(inst, algo_id)
                if r.get("force_cleanup"):
                    print(f"[warn] {inst} {algo_id} 撤单后仍残留")
                    continue
                ok += 1
                break
            except Exception as e:  # noqa: BLE001
                print(f"[retry] {inst} {algo_id} {e}")
                time.sleep(2)
    print(f"[OK] OCO撤销完成: {ok}/{total}")
    return ok, total


def close_all_positions():
    poss = okx_exec.get_positions()
    total = len([p for p in poss if abs(float(p.get("pos", 0) or 0)) > 0])
    ok = 0
    for p in poss:
        pos = float(p.get("pos", 0) or 0)
        if pos == 0:
            continue
        inst_okx = p.get("instId")
        inst = "BTC_USDT" if "BTC" in inst_okx else "XAU_USDT"
        pos_side = p.get("posSide")
        qty = abs(pos)
        side = "sell" if pos_side == "long" else "buy"
        body = {
            "instId": inst_okx,
            "tdMode": "isolated",
            "side": side,
            "posSide": pos_side,
            "ordType": "market",
            "sz": str(qty),
            "reduceOnly": "true",
        }
        for attempt in range(3):
            try:
                r = okx_exec._private_request("POST", "/api/v5/trade/order", body=body)
                row = (r.get("data") or [{}])[0]
                print(f"[OK] 平仓 {inst} {pos_side} {qty}张 -> ordId={row.get('ordId')}")
                ok += 1
                break
            except Exception as e:  # noqa: BLE001
                print(f"[retry] 平仓 {inst} {pos_side}: {e}")
                time.sleep(2)
    print(f"[OK] 平仓完成: {ok}/{total}")
    return ok, total


if __name__ == "__main__":
    c_ok, c_total = cancel_all_oco()
    p_ok, p_total = close_all_positions()
    print(f"=== 清理汇总: OCO {c_ok}/{c_total}，持仓 {p_ok}/{p_total} ===")
    sys.exit(0 if (c_ok == c_total and p_ok == p_total) else 1)
