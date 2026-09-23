#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
逐笔成交推送（OKX Demo 模拟盘）
================================
每次运行查询 OKX 最近成交（fills），与已推送状态 .fills_pushed.json 对比，
把新增成交逐笔通过 wecom-cli 直发老板个人企业微信。

用法:
  python3 trade_push.py                  # 查询增量并推送
  python3 trade_push.py --no-push        # 只打印不推送（测试）
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import okx_exec

BJ_TZ = timezone(timedelta(hours=8))
WECOM_USERID = "won22_bgAAVzd-4_LKT6VxA_Sf8lNoIw"  # Wind
WECOM_CLI = (os.environ.get("WECOM_CLI") or shutil.which("wecom-cli")
             or "/home/marvis/.npm-global/bin/wecom-cli")
PUSHED_STATE_FILE = ".fills_pushed.json"

_INST_OKX = {"BTC_USDT": "BTC-USDT-SWAP", "XAU_USDT": "XAU-USDT-SWAP"}
_LEVERAGE = {"BTC_USDT": 100, "XAU_USDT": 50}


def load_pushed(base_dir):
    path = os.path.join(base_dir, PUSHED_STATE_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_pushed(base_dir, state):
    path = os.path.join(base_dir, PUSHED_STATE_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_recent_fills():
    """拉取两个品种最近成交，返回按 ts 升序的合并列表。"""
    out = []
    for contract, inst in _INST_OKX.items():
        try:
            data = okx_exec._private_request(
                "GET", "/api/v5/trade/fills",
                params={"instType": "SWAP", "instId": inst, "limit": "100"})
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {contract} fills 查询失败: {e}")
            continue
        for o in data.get("data", []):
            o["_contract"] = contract
            out.append(o)
    out.sort(key=lambda o: int(o.get("ts") or 0))
    return out


def push_cli(text):
    cmd = [WECOM_CLI, "message", "send",
           "--chat-id", WECOM_USERID,
           "--msg-type", "text",
           "--text", json.dumps({"content": text}, ensure_ascii=False)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"wecom-cli 发送失败: {proc.stderr[:300]}")
    return proc.stdout


def format_fill(o):
    """生成单笔成交推送文案。"""
    contract = o["_contract"]
    inst = contract.replace("_USDT", "")
    side = o.get("side", "?")          # buy/sell（相对仓位方向）
    pos_side = o.get("posSide", "")    # long/short
    px = o.get("px") or o.get("avgPx")
    pnl = float(o.get("pnl") or 0.0)
    fee = float(o.get("fee") or 0.0)
    net = pnl + fee
    ts = int(o.get("ts") or 0)
    t_bj = datetime.fromtimestamp(ts / 1000, BJ_TZ).strftime("%H:%M")

    if pos_side == "short":
        arrow, word = "▼", "空"
    elif pos_side == "long":
        arrow, word = "▲", "多"
    else:  # 兼容缺失 posSide
        arrow, word = ("▼", "空") if side == "sell" else ("▲", "多")

    if pnl != 0 or fee != 0:  # 平仓成交
        flag = "✅" if net >= 0 else "❌"
        return (f"{flag} 平仓 {inst} {word} {net:+.2f}U\n"
                f"时间 {t_bj} 价格 {px} | 含费 {fee:+.2f}")
    # 开仓成交
    lev = _LEVERAGE.get(contract, 100)
    return (f"🚀 开仓 {inst} {arrow} {word} {lev}x\n"
            f"时间 {t_bj} 价格 {px} | 单笔 5U")


def main():
    parser = argparse.ArgumentParser(description="逐笔成交推送")
    parser.add_argument("--no-push", action="store_true", help="只打印不推送")
    args = parser.parse_args()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    state = load_pushed(base_dir)
    fills = fetch_recent_fills()
    new_items = []
    for o in fills:
        tid = o.get("tradeId")
        if not tid or tid in state:
            continue
        state[tid] = {"ts": o.get("ts"), "inst": o.get("instId"),
                      "side": o.get("side"), "posSide": o.get("posSide")}
        new_items.append(o)

    if not new_items:
        print("[info] 无新增成交")
        return

    sent = 0
    for o in new_items:
        text = format_fill(o)
        print(f"[trade-push] {o['tradeId']}\n{text}")
        if args.no_push:
            continue
        try:
            push_cli(text)
            sent += 1
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 推送失败 {o['tradeId']}: {e}")

    save_pushed(base_dir, state)
    print(f"[info] 新增 {len(new_items)} 笔，推送 {sent} 条")


if __name__ == "__main__":
    main()
