#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FVG 自动交易 · 每日统计与推送
==============================
统计每个自然日（北京时间 00:00~23:59）的交易情况：
  - 开单数、方向分布、杠杆、下单金额
  - 已平仓盈亏（从 OKX 成交历史统计）
  - 未平仓持仓提示

推送通道（二选一）：
  1. --push-webhook：企业微信群机器人（GitHub Actions 自动定时用）
  2. --push-cli：wecom-cli 直发授权真人（本地手动用，如老板在手机端）

用法:
  python3 daily_report.py --date 2026-09-23 --push-webhook
  python3 daily_report.py --date 2026-09-23 --push-cli
  python3 daily_report.py --date 2026-09-23            # 只输出不推送
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import okx_exec

BJ_TZ = timezone(timedelta(hours=8))
WECOM_USERID = "won22_bgAAVzd-4_LKT6VxA_Sf8lNoIw"  # Wind
WECOM_CLI = "/home/marvis/.npm-global/bin/wecom-cli"

_INST_OKX = {"BTC_USDT": "BTC-USDT-SWAP", "XAU_USDT": "XAU-USDT-SWAP"}


def day_range(day_str):
    """返回北京时间某自然日的 (start_bj, end_bj)。"""
    day = datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=BJ_TZ)
    return day, day + timedelta(days=1)


def to_ms(ts_bj):
    return int(ts_bj.timestamp() * 1000)


def fetch_orders(contract, begin_ms, end_ms):
    """拉取 OKX 当日成交明细（demo 环境 fills 可用且带 pnl；orders-history 需 instType 且 demo 支持不稳定）。
    策略：拉最近成交（limit=100），本地按 ts 过滤时间区间。返回升序成交列表。"""
    inst_okx = _INST_OKX.get(contract, contract)  # 内部代码 -> OKX SWAP 代码
    params = {"instType": "SWAP", "instId": inst_okx, "limit": "100"}
    data = okx_exec._private_request("GET", "/api/v5/trade/fills", params=params)
    orders = data.get("data", [])
    # 本地按成交时间 ts 过滤区间
    out = [o for o in orders if begin_ms <= int(o.get("ts") or 0) < end_ms]
    out.sort(key=lambda o: int(o.get("ts") or 0))
    return out


def build_report(day_str, dry=False):
    """生成某自然日交易日报文本。"""
    start_bj, end_bj = day_range(day_str)
    begin_ms, end_ms = to_ms(start_bj), to_ms(end_bj)
    lines = []
    lines.append(f"📊 FVG 自动交易日报 · {day_str}")
    lines.append("")

    total_orders = 0
    total_pnl = 0.0
    wins = 0
    losses = 0
    closed_rows = []
    per_contract = {}
    direction_stat = {"buy": 0, "sell": 0}

    for contract, inst in _INST_OKX.items():
        try:
            orders = fetch_orders(contract, begin_ms, end_ms)
        except Exception as e:  # noqa: BLE001
            lines.append(f"{contract}: 查询失败 {e}")
            continue
        if not orders:
            per_contract[contract] = 0
            continue
        # fills 为逐笔成交：开单数按 ordId 去重，方向取每个 ordId 的首笔成交 side
        ord_ids = {o.get("ordId") for o in orders}
        per_contract[contract] = len(ord_ids)
        total_orders += len(ord_ids)
        first_side = {}
        for o in orders:
            oid = o.get("ordId")
            if oid not in first_side:
                first_side[oid] = o.get("side", "?")
        for s in first_side.values():
            direction_stat[s] = direction_stat.get(s, 0) + 1
        for o in orders:
            side = o.get("side", "?")
            pnl = float(o.get("pnl") or 0.0)      # 已实现盈亏（USDT）
            fee = float(o.get("fee") or 0.0)      # 手续费（负值）
            net = pnl + fee
            total_pnl += net
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
            if pnl != 0 or fee != 0:
                closed_rows.append({
                    "contract": contract, "side": side,
                    "px": o.get("px") or o.get("avgPx"), "pnl": pnl, "fee": fee, "net": net,
                    "state": o.get("state"),
                })

    # 摘要
    lines.append(f"**开单数**：{total_orders} 笔")
    if per_contract:
        lines.append(f"**分品种**：" + " / ".join(f"{c.replace('_USDT','')} {n}" for c, n in per_contract.items()))
    lines.append(f"**方向**：多 {direction_stat.get('buy', 0)} 笔 / 空 {direction_stat.get('sell', 0)} 笔")
    lines.append(f"**已平仓**：{wins} 盈 / {losses} 亏")
    lines.append(f"**净盈亏**：<font color=\"{'info' if total_pnl >= 0 else 'warning'}\">{total_pnl:+.2f} USDT</font>")
    lines.append("")
    lines.append("明细（平仓/有费用）：")
    if not closed_rows:
        lines.append("- 当日无已平仓记录（持仓中或未成交）")
    else:
        for r in closed_rows[-10:]:
            arrow = "▲" if r["side"] == "buy" else "▼"
            lines.append(f"- {r['contract'].replace('_USDT','')} {arrow} {r['px']} 净 {r['net']:+.2f}")
    lines.append("")
    lines.append("> 模拟盘自动交易 · FVG 策略")
    text = "\n".join(lines)
    if dry:
        text += "\n\n[DRY-RUN 未推送]"
    return text


def push_webhook(text, webhook):
    """推送到企业微信群机器人。"""
    import requests
    payload = {"msgtype": "markdown", "markdown": {"content": text}}
    r = requests.post(webhook, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


def push_cli(text):
    """通过 wecom-cli 直发授权真人 Wind。"""
    cmd = [WECOM_CLI, "message", "send",
           "--chat-id", WECOM_USERID,
           "--msg-type", "text",
           "--text", json.dumps({"content": text}, ensure_ascii=False)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError(f"wecom-cli 发送失败: {proc.stderr[:300]}")
    return proc.stdout


def load_webhook(explicit=None):
    """确定群机器人 webhook：--webhook > WECOM_WEBHOOK > config.yaml。"""
    if explicit:
        return explicit
    env = os.environ.get("WECOM_WEBHOOK", "")
    if env:
        return env
    cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.yaml")
    if os.path.exists(cfg):
        import re
        with open(cfg, encoding="utf-8") as f:
            m = re.search(r'^\s*wecom_webhook\s*:\s*"?([^"\s]+)"?\s*$', f.read(), re.M)
        if m:
            return m.group(1)
    return ""


def main():
    parser = argparse.ArgumentParser(description="FVG 自动交易每日统计")
    parser.add_argument("--date", default=None, help="日期 YYYY-MM-DD（北京时间），默认今天")
    parser.add_argument("--push-webhook", action="store_true", help="推送到企业微信群机器人")
    parser.add_argument("--webhook", default=None, help="群机器人 webhook（覆盖配置）")
    parser.add_argument("--push-cli", action="store_true", help="通过 wecom-cli 直发授权真人")
    parser.add_argument("--save-dir", default=None, help="将日报保存为 daily_report_<date>.md 到指定目录")
    args = parser.parse_args()

    day_str = args.date or datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    text = build_report(day_str)
    print(text)

    if args.save_dir:
        os.makedirs(args.save_dir, exist_ok=True)
        out_path = os.path.join(args.save_dir, f"daily_report_{day_str}.md")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"[saved] {out_path}")

    sent = False
    if args.push_webhook:
        wh = load_webhook(args.webhook)
        if not wh:
            print("未找到 webhook", file=sys.stderr)
            sys.exit(2)
        resp = push_webhook(text, wh)
        print("[push-webhook]", resp)
        sent = True
    if args.push_cli:
        out = push_cli(text)
        print("[push-cli]", out[:200])
        sent = True
    if not sent:
        print("[info] 未推送，仅输出。加 --push-webhook 或 --push-cli 推送。")


if __name__ == "__main__":
    main()
