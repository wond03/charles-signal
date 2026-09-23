#!/usr/bin/env python3
"""验证+再撤 OCO：平仓后这些挂单可能已失效，重试撤销并打印原始响应"""
import json
import time

import okx_exec


def raw_post(path, body):
    """直接发私有请求并返回原始 dict（不抛异常）"""
    try:
        return okx_exec._private_request("POST", path, body=body), None
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def main():
    print("=== 1) 当前 pending OCO ===")
    pend = okx_exec.get_algo_orders(ord_type="oco")
    print(f"pending 数量: {len(pend)}")
    for a in pend[:50]:
        print(f"  {a.get('instId')} algoId={a.get('algoId')} state={a.get('state')} sz={a.get('sz')} ordType={a.get('ordType')}")

    if not pend:
        print("无需清理")
        return

    print("\n=== 2) 逐个重试 cancel-algos（打印原始响应） ===")
    ok = 0
    fail = 0
    for a in pend:
        inst_okx = a.get("instId")
        algo_id = a.get("algoId")
        body = {"algoId": [{"instId": inst_okx, "algoId": algo_id}]}
        resp, err = raw_post("/api/v5/trade/cancel-algos", body)
        if err:
            print(f"  FAIL {inst_okx} {algo_id}: {err}")
            fail += 1
        else:
            code = resp.get("code")
            msg = resp.get("msg")
            data = resp.get("data")
            if str(code) == "0":
                ok += 1
                print(f"  OK   {inst_okx} {algo_id} -> code={code} data={data}")
            else:
                fail += 1
                print(f"  FAIL {inst_okx} {algo_id} -> code={code} msg={msg!r} data={data}")
        time.sleep(0.5)

    print(f"\n=== 3) 撤后复查 pending ===")
    time.sleep(2)
    pend2 = okx_exec.get_algo_orders(ord_type="oco")
    print(f"remaining: {len(pend2)}")
    for a in pend2[:20]:
        print(f"  {a.get('instId')} algoId={a.get('algoId')} state={a.get('state')}")

    print(f"\n汇总: 撤成功 {ok}，失败 {fail}，剩余 {len(pend2)}")


if __name__ == "__main__":
    main()
