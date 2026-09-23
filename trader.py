#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FVG 信号自动交易模块（OKX Demo 模拟盘）
========================================
策略规则（按老板设定）：
  - 每次扫描出的 FVG 信号都开单，不限制信号数、不限制交易次数
  - 单仓约束：同一币种 + 同一方向（不限周期），已有未平仓则跳过新信号（平仓后可再开）
  - 同方向不叠加：防止 OKX 同向加仓导致仓位合并
  - 杠杆：BTC 100x / XAU 50x
  - 盈亏比 1:2（TP 距离 = 2 × SL 距离）
  - 固定每次下单金额 5U
  - 止盈止损用 OKX OCO 算法单（交易所端触发，OCO 数量=本次开仓量，各仓独立管理）

用法:
  python3 trader.py --contract BTC_USDT --intervals 1h,15m,5m --trade
  或通过 fvg_scanner.py --trade 复用扫描结果

凭证（优先级）:
  OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE 环境变量
  或同目录 .env_okx.json（本地开发，不入库）
"""

import argparse
import glob
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
MIN_SL_PCT = 0.003             # 最小止损距离（入场价的 0.3%），防止 FVG 过窄导致扫损
TRADED_STATE_FILE = ".fvg_traded.json"  # 持仓/已开单状态（单仓约束去重）

BJ_TZ = timezone(timedelta(hours=8))

_INST_OKX = {"BTC_USDT": "BTC-USDT-SWAP", "XAU_USDT": "XAU-USDT-SWAP"}

NOT_TRIGGERED_FILE = ".not_triggered.json"  # 未触发通知去重（防每轮刷屏）


def _load_not_triggered(base_dir):
    path = os.path.join(base_dir, NOT_TRIGGERED_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_not_triggered(base_dir, state):
    path = os.path.join(base_dir, NOT_TRIGGERED_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def notify_not_triggered(contract, desc, reason, base_dir, force=False):
    """信号已推送但自动开单未执行时，推一条'未触发'企微消息（同 key 去重）。"""
    import fvg_scanner
    state = _load_not_triggered(base_dir)
    key = desc if isinstance(desc, str) and "|" in desc else f"{contract}|{desc}"
    if not force and state.get(key):
        return False
    wh = fvg_scanner.load_webhook()
    if not wh:
        print(f"[notify] 未找到 webhook，未触发通知跳过: {key}")
        return False
    content = (
        "# ⚠️ 未触发 · " + contract + "\n\n"
        f"**信号**：<font color=\"comment\">{desc}</font>\n"
        f"**状态**：<font color=\"warning\">未触发（{reason}）</font>\n"
        f"**说明**：信号已推送但自动开单未执行，请人工检查 OKX 持仓。"
    )
    try:
        import requests
        r = requests.post(wh, json={"msgtype": "markdown", "markdown": {"content": content}}, timeout=10)
        r.raise_for_status()
        state[key] = 1
        _save_not_triggered(base_dir, state)
        print(f"[notify] 未触发已推送: {key}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[notify] 未触发推送失败: {e}")
        return False


def load_traded_state(base_dir):
    """读取状态文件（含旧格式自动迁移）。

    新格式:
      {
        "positions": {
          "合约|周期|方向": {"status": "open", "algo_id": "...", "qty": 0.59,
                          "time": "2026-09-23 20:05", "entry": 85492.8}
        },
        "opened_signals": {"合约|周期|时间": 1}
      }
    旧格式（仅时间去重）: {"合约|周期|时间": 1}
    """
    path = os.path.join(base_dir, TRADED_STATE_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {"positions": {}, "opened_signals": {}}
    if not isinstance(d, dict):
        return {"positions": {}, "opened_signals": {}}
    # 旧格式迁移：v==1 且 key 为 合约|周期|时间（无 positions 结构）
    if "positions" not in d:
        opened = {}
        for k, v in d.items():
            if isinstance(k, str) and len(k.split("|")) == 3:
                opened[k] = 1
        # 从 daily_trades 账本补建 positions（每 合约|方向 取最新一笔），
        # 使单仓约束在旧状态迁移后立即生效；对账会随后修正已平仓位
        positions = {}
        for day_path in sorted(glob.glob(os.path.join(base_dir, "daily_trades_*.json"))):
            try:
                with open(day_path, encoding="utf-8") as f:
                    rows = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(rows, list):
                continue
            for rec in rows:
                c = rec.get("contract")
                dr = rec.get("direction")
                if not c or dr not in ("long", "short"):
                    continue
                pkey = f"{c}|{dr}"
                prev = positions.get(pkey)
                t = rec.get("time", "")
                if prev is None or t > (prev.get("time") or ""):
                    positions[pkey] = {
                        "status": "open",
                        "algo_id": str(rec.get("algo_id") or ""),
                        "qty": rec.get("qty", 0),
                        "time": t,
                        "entry": rec.get("entry_ref", 0),
                    }
        return {"positions": positions, "opened_signals": opened}
    positions = d.get("positions")
    opened = d.get("opened_signals")
    return {
        "positions": positions if isinstance(positions, dict) else {},
        "opened_signals": opened if isinstance(opened, dict) else {},
    }


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


def compute_sltp(entry_price, fvg_type, bottom, top, min_sl_pct=MIN_SL_PCT):
    """按盈亏比 1:2 计算止盈止损价。
    看涨（多单）：止损在缺口下沿 bottom 下方；看跌（空单）：止损在缺口上沿 top 上方。
    SL 距离 = 缺口边界到入场价距离；TP = 入场 ± 2 × SL 距离。
    若缺口过窄（SL 距离 < 入场价 × min_sl_pct），按最小距离外扩止损，防扫损。
    """
    if fvg_type == "bullish":
        sl = bottom
        sl_dist = entry_price - sl
        if sl_dist <= 0:
            sl_dist = abs(entry_price - bottom) or entry_price * 0.001
            sl = entry_price - sl_dist
        min_dist = entry_price * min_sl_pct
        if sl_dist < min_dist:
            sl_dist = min_dist
            sl = entry_price - sl_dist
        tp = entry_price + RR * sl_dist
        return round(sl, 4), round(tp, 4)
    else:  # bearish
        sl = top
        sl_dist = sl - entry_price
        if sl_dist <= 0:
            sl_dist = abs(top - entry_price) or entry_price * 0.001
            sl = entry_price + sl_dist
        min_dist = entry_price * min_sl_pct
        if sl_dist < min_dist:
            sl_dist = min_dist
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

    # OCO 止盈止损：数量=本次开仓量，各仓独立挂单、互不覆盖（不做"撤旧挂新"）
    oco_qty = order.get("qty", 0)

    algo = {}
    try:
        algo = okx_exec.place_algo_sltp(contract, direction, oco_qty, sl, tp)
        print(f"[trade] OCO止盈止损挂单成功 algoId={algo.get('algoId')} qty={oco_qty}")
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


def reconcile_positions(base_dir, contract, dry_run=False):
    """对账持仓状态：把已平掉的 open 记录标记为 closed。

    判定依据（取其一即视为已平）：
      1) 该方向 OKX 总持仓为 0（查询成功时）；
      2) 记录里的 OCO algoId 已不在交易所 pending 列表（OCO 触发即平仓）。
    查询失败时保持原状，宁可漏判也不误标（避免同周期同方向重复开仓）。
    """
    if dry_run:
        return
    state = load_traded_state(base_dir)
    positions = state["positions"]
    if not positions:
        return
    changed = False
    # 该合约相关记录
    keys = [k for k in positions if k.startswith(contract + "|")]
    if not keys:
        return
    # 1) pending OCO 集合
    pend_ids = set()
    try:
        pend = okx_exec.get_algo_orders(inst=contract, ord_type="oco")
        pend_ids = {str(a.get("algoId")) for a in pend if a.get("algoId")}
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 查询pending OCO失败(对账降级): {e}")
    # 2) 各方向持仓张数
    poss_ok = False
    pos_by_dir = {}
    try:
        poss = okx_exec.get_positions(contract)
        poss_ok = True
        for p in poss:
            if abs(float(p.get("pos", 0) or 0)) > 0:
                d = p.get("posSide")
                pos_by_dir[d] = pos_by_dir.get(d, 0) + abs(float(p.get("pos", 0)))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 查询持仓失败(对账降级): {e}")
    for k in keys:
        rec = positions[k]
        if not isinstance(rec, dict) or rec.get("status") != "open":
            continue
        parts = k.split("|")
        # 兼容新旧两种格式：合约|方向（2段） 与 合约|周期|方向（3段，迁移残留）
        direction = parts[1] if len(parts) == 2 else parts[2]
        algo_id = str(rec.get("algo_id") or "")
        if poss_ok and pos_by_dir.get(direction, 0) == 0:
            rec["status"] = "closed"
            changed = True
            print(f"[trade] 对账: {k} 已平仓(持仓为0)")
        elif algo_id and algo_id not in pend_ids:
            rec["status"] = "closed"
            changed = True
            print(f"[trade] 对账: {k} 已平仓(OCO已触发/撤销)")
    if changed:
        save_traded_state(base_dir, state)


def has_open_position(contract, direction, state):
    """单仓约束：同品种+同方向（不限周期）是否已有未平仓。

    兼容新旧两种状态 key：
      - 新格式: 合约|方向（如 XAU_USDT|long）
      - 旧格式: 合约|周期|方向（如 XAU_USDT|5m|long，迁移残留）
    """
    for key, rec in state["positions"].items():
        if not isinstance(rec, dict) or rec.get("status") != "open":
            continue
        parts = key.split("|")
        if len(parts) == 3 and parts[0] == contract and parts[2] == direction:
            return True
        if len(parts) == 2 and parts[0] == contract and parts[1] == direction:
            return True
    return False


def trade_all_fvgs(contract, intervals, window_start, window_end, min_gap=1.0,
                   base_dir=None, dry_run=False):
    """扫描 FVG 并对新信号开单（单仓约束：同币种+同方向不重复开，不限周期）。
    返回开单汇总。"""
    import fvg_scanner
    result = fvg_scanner.scan(contract, intervals, window_start, window_end, min_gap)
    reconcile_positions(base_dir, contract, dry_run=dry_run)
    state = load_traded_state(base_dir)
    opened = []
    for interval in intervals:
        info = result["intervals"].get(interval, {})
        if "error" in info or not info.get("fvgs"):
            continue
        for f in info["fvgs"]:
            direction = "long" if f["type"] == "bullish" else "short"
            # 同一信号只开一次（按 合约|周期|时间）
            sig_key = f"{contract}|{interval}|{f['time']}"
            if state["opened_signals"].get(sig_key):
                continue  # 已开单，跳过
            # 单仓约束：同币种+同方向（跨周期）已有未平仓 → 跳过，防止 OKX 同向合并
            if has_open_position(contract, direction, state):
                print(f"[trade] 跳过: {contract} {direction} 已有未平仓(单仓约束,不限周期)")
                notify_not_triggered(contract, sig_key, "同方向持仓中，单仓约束跳过开单", base_dir)
                continue
            f2 = dict(f)
            f2["interval"] = interval
            print(f"[trade] 发现新FVG: {contract} {interval} {f['time']} {f['type']}")
            r = open_position(contract, f2, base_dir, dry_run=dry_run)
            opened.append({"key": sig_key, "result": r})
            if not r.get("ok") and not dry_run:
                notify_not_triggered(contract, sig_key, f"开单失败: {r.get('reason')}", base_dir)
            if r.get("ok") and not dry_run:  # 仅真实开单成功才记录，失败/演练保留以便下轮重试
                state["opened_signals"][sig_key] = 1
                pos_key = f"{contract}|{direction}"
                state["positions"][pos_key] = {
                    "status": "open",
                    "algo_id": (r.get("algo") or {}).get("algoId", ""),
                    "qty": (r.get("order") or {}).get("qty", 0),
                    "time": f["time"],
                    "entry": r.get("entry_ref", 0),
                }
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
        save_traded_state(base_dir, {"positions": {}, "opened_signals": {}})
        print(f"[trade] 已清空持仓/去重状态（{len(state['positions'])} 仓, {len(state['opened_signals'])} 信号）")

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
    try:
        res = trade_all_fvgs(args.contract, intervals, window_start, window_end,
                             args.min_gap, base_dir=base_dir, dry_run=args.dry_run)
    except Exception as e:  # noqa: BLE001
        print(f"[trade] 开单流程异常: {type(e).__name__}: {e}", file=sys.stderr)
        notify_not_triggered(args.contract, f"{args.contract} 开单流程异常",
                             f"{type(e).__name__}: {e}", base_dir)
        raise
    print(f"[trade] 新开单数: {res['new_count']}")
    for item in res["opened"]:
        r = item["result"]
        if r.get("ok"):
            print(f"  ✔ {item['key']} entry={r.get('entry_ref')} sl={r.get('sl')} tp={r.get('tp')}")
        else:
            print(f"  ✘ {item['key']} 失败: {r.get('reason')}")


if __name__ == "__main__":
    main()

