#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FVG 信号自动交易模块（OKX Demo 模拟盘）
========================================
策略规则（按老板设定）：
  - 每次扫描出的 FVG 信号都开单，不限制信号数、不限制交易次数、不限制周期
  - 杠杆：BTC 100x / XAU 50x
  - 盈亏比 1:2（TP 距离 = 2 × SL 距离）
  - 固定每次下单金额 5U
  - 止盈止损用 OKX OCO 算法单（交易所端触发）

用法:
  python3 trader.py --contract BTC_USDT --intervals 1h,15m,5m --trade
  或通过 fvg_scanner.py --trade 复用扫描结果

凭证（优先级）:
  OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE 环境变量
  或同目录 .env_okx.json（本地开发，不入库）
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import okx_exec

# 交易参数（老板设定）
TRADE_SIZE_USDT = 5.0          # 每次固定 5U
LEVERAGE = {"BTC_USDT": 100, "XAU_USDT": 50}   # BTC 100x / XAU 50x
RR = 2.0                       # 盈亏比 1:2（TP = 2 × SL）
TRADED_STATE_FILE = ".fvg_traded.json"  # 已开单去重状态

BJ_TZ = timezone(timedelta(hours=8))

_INST_OKX = {"BTC_USDT": "BTC-USDT-SWAP", "XAU_USDT": "XAU-USDT-SWAP"}


def load_traded_state(base_dir):
    path = os.path.join(base_dir, TRADED_STATE_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_traded_state(base_dir, state):
    path = os.path.join(base_dir, TRADED_STATE_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def record_trade(base_dir, rec):
    """把交易记录追加写入 daily_trades.json（自然日分文件）。"""
    day = rec["time"][:10]
    path = os.path.join(base_dir, f"daily_trades_{day}.json")
    rows = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                rows = json.load(f)
                if not isinstance(rows, list):
                    rows = []
        except (OSError, ValueError):
            rows = []
    rows.append(rec)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    return path


def compute_sltp(entry_price, fvg_type, bottom, top):
    """按盈亏比 1:2 计算止盈止损价。
    看涨（多单）：止损在缺口下沿 bottom 下方；看跌（空单）：止损在缺口上沿 top 上方。
    SL 距离 = 缺口边界到入场价距离；TP = 入场 ± 2 × SL 距离。
    """
    if fvg_type == "bullish":
        sl = bottom
        sl_dist = entry_price - sl
        if sl_dist <= 0:
            sl_dist = abs(entry_price - bottom) or entry_price * 0.001
            sl = entry_price - sl_dist
        tp = entry_price + RR * sl_dist
        return round(sl, 4), round(tp, 4)
    else:  # bearish
        sl = top
        sl_dist = sl - entry_price
        if sl_dist <= 0:
            sl_dist = abs(top - entry_price) or entry_price * 0.001
            sl = entry_price + sl_dist
        tp = entry_price - RR * sl_dist
        return round(sl, 4), round(tp, 4)


def open_position(contract, fvg, base_dir, dry_run=False):
    """对单个 FVG 信号开单：设杠杆 → 市价开仓 → 挂 OCO 止盈止损 → 记录。"""
    inst_okx = _INST_OKX[contract]
    side = "buy" if fvg["type"] == "bullish" else "sell"
    direction = "long" if fvg["type"] == "bullish" else "short"
    lev = LEVERAGE.get(contract, 100)

    # 当前市价（作为开仓参考价）；dry-run 用 FVG 缺口均值代替，避免请求行情
    if dry_run:
        entry_price = round((fvg["bottom"] + fvg["top"]) / 2, 8)
    else:
        klines = okx_exec.fetch_klines(contract, "1m", 3)
        if not klines:
            return {"ok": False, "reason": "无法获取市价"}
        entry_price = float(klines[-1]["close"])
    print(f"[trade] {contract} {fvg['type']} @ {fvg['time']} 参考价 {entry_price} 杠杆 {lev}x")

    # 止盈止损
    sl, tp = compute_sltp(entry_price, fvg["type"], fvg["bottom"], fvg["top"])
    print(f"[trade] SL={sl} TP={tp} (RR 1:{RR:.0f})")

    if dry_run:
        return {
            "ok": True, "dry_run": True,
            "contract": contract, "side": side, "direction": direction,
            "entry_ref": entry_price, "sl": sl, "tp": tp,
            "size_usdt": TRADE_SIZE_USDT, "leverage": lev,
        }

    # 设杠杆
    try:
        okx_exec.set_leverage(contract, lev)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 设置杠杆失败(继续): {e}")

    # 市价开仓
    try:
        order = okx_exec.place_market_order(contract, side, TRADE_SIZE_USDT,
                                            entry_price, leverage=lev)
    except Exception as e:  # noqa: BLE001
        print(f"[error] 开仓失败: {e}")
        return {"ok": False, "reason": str(e)}
    print(f"[trade] 开仓成功 ordId={order.get('ordId')} qty={order.get('qty')}")

    # 挂 OCO 止盈止损
    algo = {}
    try:
        algo = okx_exec.place_algo_sltp(contract, direction, order.get("qty", 0), sl, tp)
        print(f"[trade] OCO止盈止损挂单成功 algoId={algo.get('algoId')}")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 挂止盈止损失败(持仓已开,注意手动处理): {e}")

    rec = {
        "time": fvg["time"],
        "ts": fvg.get("ts"),
        "contract": contract,
        "interval": fvg.get("interval", ""),
        "type": fvg["type"],
        "side": side,
        "direction": direction,
        "entry_ref": entry_price,
        "sl": sl,
        "tp": tp,
        "size_usdt": TRADE_SIZE_USDT,
        "leverage": lev,
        "ord_id": order.get("ordId"),
        "qty": order.get("qty"),
        "algo_id": algo.get("algoId", ""),
    }
    path = record_trade(base_dir, rec)
    print(f"[trade] 记录已写入 {path}")
    return {"ok": True, "order": order, "algo": algo, "record": rec}


def trade_all_fvgs(contract, intervals, window_start, window_end, min_gap=1.0,
                   base_dir=None, dry_run=False):
    """扫描 FVG 并对每个新信号开单（不限制数量/周期）。返回开单汇总。"""
    import fvg_scanner
    result = fvg_scanner.scan(contract, intervals, window_start, window_end, min_gap)
    state = load_traded_state(base_dir)
    opened = []
    for interval in intervals:
        info = result["intervals"].get(interval, {})
        if "error" in info or not info.get("fvgs"):
            continue
        for f in info["fvgs"]:
            key = f"{contract}|{interval}|{f['time']}"
            if state.get(key):
                continue  # 已开单，跳过
            f2 = dict(f)
            f2["interval"] = interval
            print(f"[trade] 发现新FVG: {contract} {interval} {f['time']} {f['type']}")
            r = open_position(contract, f2, base_dir, dry_run=dry_run)
            opened.append({"key": key, "result": r})
            state[key] = 1
            save_traded_state(base_dir, state)
            if not dry_run:
                time.sleep(1)  # 避免 OKX 限频
    return {"contract": contract, "opened": opened,
            "new_count": len(opened), "state_saved": True}


def main():
    parser = argparse.ArgumentParser(description="FVG 信号自动交易（OKX Demo 模拟盘）")
    parser.add_argument("--contract", default="BTC_USDT", choices=list(_INST_OKX))
    parser.add_argument("--intervals", default="1h,15m,5m")
    parser.add_argument("--date", default=None, help="分析日 YYYY-MM-DD（北京时间），默认最近窗口")
    parser.add_argument("--min-gap", type=float, default=1.0)
    parser.add_argument("--dry-run", action="store_true", help="只计算不下单（测试）")
    parser.add_argument("--reset", action="store_true", help="清空已开单去重状态（重新全量开单）")
    args = parser.parse_args()

    base_dir = os.path.dirname(os.path.abspath(__file__))
    if args.reset:
        state = load_traded_state(base_dir)
        save_traded_state(base_dir, {})
        print(f"[trade] 已清空去重状态（{len(state)} 条）")

    intervals = [s.strip() for s in args.intervals.split(",") if s.strip()]
    now_bj = datetime.now(BJ_TZ)
    if args.date:
        day = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=BJ_TZ)
        window_start = day.replace(hour=8, minute=0, second=0, microsecond=0)
        window_end = window_start + timedelta(hours=21)
    else:
        import fvg_scanner
        window_start, window_end = fvg_scanner.get_window(now_bj)

    print(f"[trade] 窗口: {window_start:%Y-%m-%d %H:%M} ~ {window_end:%Y-%m-%d %H:%M} (北京时间)")
    res = trade_all_fvgs(args.contract, intervals, window_start, window_end,
                         args.min_gap, base_dir=base_dir, dry_run=args.dry_run)
    print(f"[trade] 新开单数: {res['new_count']}")
    for item in res["opened"]:
        r = item["result"]
        if r.get("ok"):
            print(f"  ✔ {item['key']} entry={r.get('entry_ref')} sl={r.get('sl')} tp={r.get('tp')}")
        else:
            print(f"  ✘ {item['key']} 失败: {r.get('reason')}")


if __name__ == "__main__":
    main()
