#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FVG 信号的识别与检测
====================
数据源 : Gate.io USDT 本位永续合约 (公共行情接口，无需密钥)
周期   : 1H / 15M / 5M（每个周期独立扫描）
分析窗口: 北京时间每日 08:00 ～ 次日 05:00（逐根扫描）
FVG 定义: 三根K线模型
  - 看涨 FVG: 第1根高点 < 第3根低点, 缺口区间 [第1根高点, 第3根低点]
  - 看跌 FVG: 第1根低点 > 第3根高点, 缺口区间 [第3根高点, 第1根低点]
  - gap 阈值: gap >= 1（价格单位）才算有效；0.x 小数级别（如 0.2）不算

用法示例:
  python3 fvg_scanner.py --contract BTC_USDT                 # 默认 5m/15m/1h 三周期
  python3 fvg_scanner.py --contract XAU_USDT --intervals 15m
  python3 fvg_scanner.py --contract BTC_USDT --date 2026-09-23   # 指定分析日
  python3 fvg_scanner.py --contract BTC_USDT --push          # 命中 FVG 时推送企业微信（含周期与时间）
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

GATE_URL = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
TIMEOUT = 15
PUSHED_STATE_FILE = ".fvg_pushed.json"  # 已推送去重状态（脚本同目录）

BJ_TZ = timezone(timedelta(hours=8))
UTC_TZ = timezone.utc

INTERVALS = ("1h", "15m", "5m")
CONTRACTS = ("BTC_USDT", "XAU_USDT")
MIN_GAP = 1.0  # gap 阈值：>=1 才算，0.x 小数不算


def fetch_klines(contract, interval, limit=1000, to_ts=None):
    """拉取 Gate.io 永续K线，返回按时间升序的列表。"""
    params = {"contract": contract, "interval": interval, "limit": limit}
    if to_ts:
        params["to"] = str(int(to_ts))
    r = requests.get(GATE_URL, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError(f"gateio 返回异常: {data}")
    klines = []
    for d in data:
        try:
            klines.append({
                "ts": int(d["t"]),
                "open": float(d["o"]),
                "high": float(d["h"]),
                "low": float(d["l"]),
                "close": float(d["c"]),
            })
        except (KeyError, ValueError, TypeError) as e:
            print(f"[warn] 跳过异常K线 {d}: {e}", file=sys.stderr)
    klines.sort(key=lambda x: x["ts"])
    return klines


def detect_fvg(klines, min_gap=MIN_GAP):
    """三K线 FVG 检测。

    看涨: k1.high < k3.low    → gap = k3.low - k1.high
    看跌: k1.low > k3.high    → gap = k1.low - k3.high
    gap < min_gap 的缺口不计。
    返回按时间升序的 FVG 列表。
    """
    fvgs = []
    for i in range(2, len(klines)):
        k1, k2, k3 = klines[i - 2], klines[i - 1], klines[i]
        # 看涨
        if k3["low"] > k1["high"]:
            gap = k3["low"] - k1["high"]
            if gap >= min_gap:
                fvgs.append({
                    "ts": k3["ts"],
                    "k1_ts": k1["ts"],
                    "k3_ts": k3["ts"],
                    "type": "bullish",
                    "bottom": round(k1["high"], 8),
                    "top": round(k3["low"], 8),
                    "gap": round(gap, 8),
                })
        # 看跌
        if k1["low"] > k3["high"]:
            gap = k1["low"] - k3["high"]
            if gap >= min_gap:
                fvgs.append({
                    "ts": k3["ts"],
                    "k1_ts": k1["ts"],
                    "k3_ts": k3["ts"],
                    "type": "bearish",
                    "bottom": round(k3["high"], 8),
                    "top": round(k1["low"], 8),
                    "gap": round(gap, 8),
                })
    fvgs.sort(key=lambda x: x["ts"])
    return fvgs


def bj_str(ts, fmt="%Y-%m-%d %H:%M"):
    return datetime.fromtimestamp(ts, BJ_TZ).strftime(fmt)


def get_window(now_bj):
    """返回最近的 08:00～次日05:00 窗口（北京时间）。"""
    today8 = now_bj.replace(hour=8, minute=0, second=0, microsecond=0)
    if now_bj < today8:
        today8 -= timedelta(days=1)
    end = today8 + timedelta(hours=21)  # 次日 05:00
    return today8, end


def klines_in_window(klines, start_bj, end_bj):
    start_ts = int(start_bj.timestamp())
    end_ts = int(end_bj.timestamp())
    return [k for k in klines if start_ts <= k["ts"] < end_ts]


def scan(contract, intervals, window_start, window_end, min_gap=MIN_GAP):
    """对指定合约与周期执行 FVG 扫描，返回汇总 dict。"""
    result = {"contract": contract, "window": {
        "start": bj_str(window_start.timestamp(), "%Y-%m-%d %H:%M"),
        "end": bj_str(window_end.timestamp(), "%Y-%m-%d %H:%M"),
    }, "intervals": {}}
    end_ts = int(window_end.timestamp()) + 1
    for interval in intervals:
        try:
            klines = fetch_klines(contract, interval, limit=1000, to_ts=end_ts)
        except Exception as e:
            result["intervals"][interval] = {"error": str(e)}
            continue
        win_klines = klines_in_window(klines, window_start, window_end)
        fvgs = detect_fvg(win_klines, min_gap)
        bullish = [f for f in fvgs if f["type"] == "bullish"]
        bearish = [f for f in fvgs if f["type"] == "bearish"]
        result["intervals"][interval] = {
            "kline_count": len(win_klines),
            "fvg_count": len(fvgs),
            "bullish": len(bullish),
            "bearish": len(bearish),
            "fvgs": [
                {
                    "time": bj_str(f["ts"]),
                    "type": f["type"],
                    "bottom": f["bottom"],
                    "top": f["top"],
                    "gap": f["gap"],
                }
                for f in fvgs
            ],
        }
    return result


def format_text(result):
    lines = []
    lines.append(f"FVG 扫描: {result['contract']}")
    lines.append(f"窗口: {result['window']['start']} ~ {result['window']['end']} (北京时间)")
    lines.append("")
    for interval, info in result["intervals"].items():
        lines.append(f"--- {interval} ---")
        if "error" in info:
            lines.append(f"  错误: {info['error']}")
            continue
        lines.append(f"  K线数: {info['kline_count']}  FVG总数: {info['fvg_count']}"
                     f"  (看涨 {info['bullish']} / 看跌 {info['bearish']})")
        for f in info["fvgs"]:
            arrow = "▲" if f["type"] == "bullish" else "▼"
            lines.append(f"  {arrow} {f['time']} {f['type']:<8} "
                         f"区间[{f['bottom']}, {f['top']}] gap={f['gap']}")
        lines.append("")
    return "\n".join(lines)


def load_webhook(explicit=None):
    """确定企业微信 webhook：
    优先级：--webhook > 环境变量 WECOM_WEBHOOK > 脚本同目录 config.yaml 的 wecom_webhook。
    """
    if explicit:
        return explicit
    env = os.environ.get("WECOM_WEBHOOK")
    if env:
        return env
    base = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(base, "config.yaml"), "config.yaml"):
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    text = f.read()
                m = re.search(r'^\s*wecom_webhook\s*:\s*"?([^"\s]+)"?\s*$', text, re.M)
                if m:
                    return m.group(1)
            except OSError:
                pass
    return None


def send_single(webhook, contract, interval, f):
    """单个 FVG 推送一条企业微信 markdown 消息（含周期与时间）。"""
    arrow = "▲ 看涨" if f["type"] == "bullish" else "▼ 看跌"
    color = "info" if f["type"] == "bullish" else "comment"
    content = (
        f"# 🔔 FVG 信号 · {contract}\n\n"
        f"**周期**：{interval.upper()}\n"
        f"**时间**：<font color=\"warning\">{f['time']}</font>\n"
        f"**方向**：<font color=\"{color}\">{arrow}</font>\n"
        f"**区间**：`{f['bottom']} ~ {f['top']}`"
    )
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    r = requests.post(webhook, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def send_resonance(webhook, contract, time_str, items):
    """多周期同一时间的 FVG 合并为一条共振消息（多周期共振）。"""
    lines = [f"# ⚡ FVG 共振 · {contract}", "",
             f"**时间**：<font color=\"warning\">{time_str}</font>", ""]
    for interval, f in items:
        arrow = "▲ 看涨" if f["type"] == "bullish" else "▼ 看跌"
        color = "info" if f["type"] == "bullish" else "comment"
        lines.append(f"> **{interval.upper()}** <font color=\"{color}\">{arrow}</font> · `{f['bottom']} ~ {f['top']}`")
    lines += ["", "多周期共振，信号增强"]
    payload = {"msgtype": "markdown", "markdown": {"content": "\n".join(lines)}}
    r = requests.post(webhook, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def load_pushed_state():
    """读取已推送 FVG 的去重状态（contract|interval|ts -> 1）。"""
    base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, PUSHED_STATE_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_pushed_state(state):
    """写入已推送 FVG 的去重状态。"""
    base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, PUSHED_STATE_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(description="FVG 信号的识别与检测 (Gate.io 数据源)")
    parser.add_argument("--contract", default="BTC_USDT", choices=CONTRACTS,
                        help="合约，默认 BTC_USDT")
    parser.add_argument("--intervals", default=",".join(INTERVALS),
                        help="周期列表，逗号分隔，默认 1h,15m,5m")
    parser.add_argument("--date", default=None,
                        help="分析日 YYYY-MM-DD（北京时间），默认最近窗口")
    parser.add_argument("--min-gap", type=float, default=MIN_GAP,
                        help="gap 阈值，默认 1.0（0.x 小数不计）")
    parser.add_argument("--push", action="store_true",
                        help="命中 FVG 时推送企业微信（含周期与时间）")
    parser.add_argument("--webhook", default=None,
                        help="企业微信 webhook；默认按 WECOM_WEBHOOK 环境变量或 config.yaml 的 wecom_webhook")
    parser.add_argument("--no-dedup", action="store_true",
                        help="关闭推送去重（默认同一 FVG 只推送一次，防刷屏）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    intervals = [s.strip() for s in args.intervals.split(",") if s.strip()]
    for iv in intervals:
        if iv not in INTERVALS:
            print(f"不支持的周期: {iv}（支持 {', '.join(INTERVALS)}）", file=sys.stderr)
            sys.exit(1)

    now_bj = datetime.now(BJ_TZ)
    if args.date:
        try:
            day = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=BJ_TZ)
        except ValueError:
            print("日期格式应为 YYYY-MM-DD", file=sys.stderr)
            sys.exit(1)
        window_start = day.replace(hour=8, minute=0, second=0, microsecond=0)
        window_end = window_start + timedelta(hours=21)
    else:
        window_start, window_end = get_window(now_bj)

    result = scan(args.contract, intervals, window_start, window_end, args.min_gap)

    if args.push:
        webhook = load_webhook(args.webhook)
        if not webhook:
            print("未找到企业微信 webhook（可用 --webhook 指定或设置 WECOM_WEBHOOK）",
                  file=sys.stderr)
            sys.exit(2)
        state = load_pushed_state() if not args.no_dedup else {}
        # 按时间收集待推送 FVG（先去重）：{时间: [(周期, fvg), ...]}
        by_time = defaultdict(list)
        for interval in intervals:
            info = result["intervals"].get(interval, {})
            if "error" in info or not info.get("fvgs"):
                continue
            if not args.no_dedup:
                fresh = [f for f in info["fvgs"]
                         if state.get(f"{result['contract']}|{interval}|{f['time']}") is None]
            else:
                fresh = info["fvgs"]
            for f in fresh:
                by_time[f["time"]].append((interval, f))

        pushed_any = False
        for time_str in sorted(by_time):
            items = by_time[time_str]
            if len(items) >= 2:
                resp = send_resonance(webhook, result["contract"], time_str, items)
                ok = 1 if isinstance(resp, dict) and resp.get("errcode") == 0 else 0
                print(f"[push] {result['contract']} {time_str}: 共振 {len(items)} 周期, 成功 {ok}")
            else:
                interval, f = items[0]
                resp = send_single(webhook, result["contract"], interval, f)
                ok = 1 if isinstance(resp, dict) and resp.get("errcode") == 0 else 0
                print(f"[push] {result['contract']} {interval}: 1 条, 成功 {ok}")
            if not args.no_dedup:
                for interval, f in items:
                    state[f"{result['contract']}|{interval}|{f['time']}"] = 1
            pushed_any = True
        if not args.no_dedup:
            save_pushed_state(state)
        if not pushed_any:
            print("[push] 本窗口无新 FVG，未推送")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_text(result))


if __name__ == "__main__":
    main()
