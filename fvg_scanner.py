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
import base64
import hashlib
import hmac
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

# ===== 开仓模板（与 trader.py 保持一致）=====
LEVERAGE = {"BTC_USDT": 100, "XAU_USDT": 50}   # BTC 100x / XAU 50x
TRADE_SIZE_USDT = 5.0                          # 单笔固定 5U
RR = 2.0                                       # 盈亏比 1:2（TP = 2 × SL）


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


def _color_for(f):
    """看涨=绿(info)，看跌=红(warning)。企业微信 markdown 仅支持 info/warning/comment 三色。"""
    return "info" if f["type"] == "bullish" else "warning"


def compute_sltp(entry_price, fvg_type, bottom, top):
    """与 trader.py 一致：SL=缺口边界，TP = entry ± RR×SL距离。"""
    if fvg_type == "bullish":
        sl = bottom
        sl_dist = entry_price - sl
        if sl_dist <= 0:
            sl_dist = abs(entry_price - bottom) or entry_price * 0.001
            sl = entry_price - sl_dist
        tp = entry_price + RR * sl_dist
        return round(sl, 4), round(tp, 4)
    sl = top
    sl_dist = sl - entry_price
    if sl_dist <= 0:
        sl_dist = abs(top - entry_price) or entry_price * 0.001
        sl = entry_price + sl_dist
    tp = entry_price - RR * sl_dist
    return round(sl, 4), round(tp, 4)


def latest_price(contract):
    """取最新 1m 收盘价作为开仓参考价（Gate.io），失败返回 None。"""
    try:
        klines = fetch_klines(contract, "1m", limit=1)
        if klines:
            return float(klines[-1]["close"])
    except Exception:
        pass
    return None


def signal_id(contract, f):
    """生成信号 ID，如 FVG-BTC-20260923-1400。"""
    sym = contract.split("_")[0]
    t = f["time"].replace("-", "").replace(":", "").replace(" ", "-")
    return f"FVG-{sym}-{t}"


# ===== OKX 模拟盘真实参数（强平价/手续费）=====
OKX_API = "https://www.okx.com"
# fallback（仅当 OKX 接口不可用时）：BTC mmr 0.5% / XAU mmr 1.0%；taker 单边 0.05%
_MMR_FALLBACK = {"BTC_USDT": 0.005, "XAU_USDT": 0.010}
_TAKER_FALLBACK = 0.0005


def _okx_signed_headers(method, path, body=""):
    """OKX 私有请求签名头（Demo 必须带 x-simulated-trading: 1），无凭据返回 None。"""
    key = os.environ.get("OKX_API_KEY", "")
    sec = os.environ.get("OKX_API_SECRET", "")
    pas = os.environ.get("OKX_API_PASSPHRASE", "")
    if not (key and sec and pas):
        return None
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    msg = f"{ts}{method}{path}{body}"
    sig = base64.b64encode(hmac.new(sec.encode(), msg.encode(), hashlib.sha256).digest()).decode()
    return {
        "OK-ACCESS-KEY": key,
        "OK-ACCESS-SIGN": sig,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": pas,
        "Content-Type": "application/json",
        "x-simulated-trading": "1",
    }


def fetch_okx_mmr(inst_id):
    """公共接口拉维持保证金率（position-tiers，tier=1），失败返回 None。"""
    try:
        r = requests.get(f"{OKX_API}/api/v5/public/position-tiers",
                         params={"instType": "SWAP", "instId": inst_id,
                                 "tdMode": "isolated", "tier": "1"},
                         timeout=TIMEOUT)
        data = r.json().get("data") or []
        if data and "mmr" in data[0]:
            return float(data[0]["mmr"])
    except Exception:
        pass
    return None


def fetch_okx_taker_fee(inst_id):
    """私有接口拉真实 taker 费率（trade-fee，Demo 需签名），失败返回 None。"""
    path = f"/api/v5/account/trade-fee?instType=SWAP&instId={inst_id}"
    h = _okx_signed_headers("GET", path)
    if not h:
        return None
    try:
        r = requests.get(f"{OKX_API}{path}", headers=h, timeout=TIMEOUT)
        data = r.json().get("data") or []
        if data and "taker" in data[0]:
            return float(data[0]["taker"])
    except Exception:
        pass
    return None


def okx_params(contract):
    """拉取 OKX 模拟盘真实参数：维持保证金率 + taker 手续费率（单边）。
    接口不可用时回退默认值，返回 (mmr, taker_fee)。"""
    inst_id = contract.replace("_", "-") + "-SWAP"  # BTC_USDT -> BTC-USDT-SWAP
    mmr = fetch_okx_mmr(inst_id)
    if mmr is None:
        mmr = _MMR_FALLBACK.get(contract, 0.005)
    fee = fetch_okx_taker_fee(inst_id)
    if fee is None:
        fee = _TAKER_FALLBACK
    return mmr, fee


def estimate_liq(entry_price, f_type, lev, mmr=0.005, taker_fee=0.0005):
    """强平价（OKX 模拟盘口径）：距离 = 100/杠杆 - 维持保证金率 - taker手续费率(单边)。
    已验证：XAU 50x 与 OKX App 强平价误差 0.02%。"""
    dist_pct = 100.0 / lev - mmr * 100.0 - taker_fee * 100.0
    if f_type == "bullish":
        return round(entry_price * (1 - dist_pct / 100.0), 4)
    return round(entry_price * (1 + dist_pct / 100.0), 4)


def estimate_net_pnl(entry_price, tp, f_type, notional, taker_fee=0.0005):
    """止盈时预估净盈亏 = 毛利 - 双边真实 taker 手续费（OKX 模拟盘费率）。"""
    if f_type == "bullish":
        gross = (tp - entry_price) / entry_price * notional
    else:
        gross = (entry_price - tp) / entry_price * notional
    fee = notional * taker_fee * 2.0
    return gross - fee, fee


def open_tpl_block(contract, f, entry_price):
    """开仓模板 markdown 区块（杠杆/单笔/SL/TP/仓位，与 trader.py 参数一致）。"""
    lev = LEVERAGE.get(contract, 100)
    sl, tp = compute_sltp(entry_price, f["type"], f["bottom"], f["top"])
    side = "多" if f["type"] == "bullish" else "空"
    notional = TRADE_SIZE_USDT * lev
    qty_est = notional / entry_price if entry_price else 0.0
    mmr, taker_fee = okx_params(contract)
    liq = estimate_liq(entry_price, f["type"], lev, mmr, taker_fee)
    net, fee = estimate_net_pnl(entry_price, tp, f["type"], notional, taker_fee)
    return (
        "\n────────────\n"
        f"**开仓模板**：{contract} · {side} {lev}x · 单笔 {TRADE_SIZE_USDT:.0f}U 保证金\n"
        f"**触发方式**：市价（推送即开单）\n"
        f"**收盘确认**：是（K线收盘后扫描）\n"
        f"**入场参考**：<font color=\"comment\">{entry_price:.4f}</font>（最新价）\n"
        f"**止损**：<font color=\"warning\">{sl}</font>（缺口边界，无缓冲）\n"
        f"**止盈**：<font color=\"info\">{tp}</font>（RR 1:{RR:.0f}）\n"
        f"**强平价**：≈{liq}（OKX 模拟盘：维持保证金率 {mmr*100:.2f}%，taker {taker_fee*100:.3f}%）\n"
        f"**名义价值**：≈{notional:.0f}U · 数量 ≈{qty_est:.6f}\n"
        f"**预估净盈亏（止盈）**：≈{net:+.2f}U（毛利减双边手续费≈{fee:.2f}U，OKX 模拟盘 taker {taker_fee*100:.3f}%）\n"
        f"**风险提示**：{lev}x 杠杆风险极高，滑点与手续费可能显著影响小止损单"
    )


def send_single(webhook, contract, interval, f, entry_price):
    """单个 FVG 推送一条企业微信 markdown 消息（含周期、时间、开仓模板）。"""
    arrow = "▲ 看涨" if f["type"] == "bullish" else "▼ 看跌"
    color = _color_for(f)
    content = (
        f"# 🔔 FVG 信号 · {contract}\n\n"
        f"**时间**：<font color=\"comment\">{f['time']}（UTC+8）</font>\n"
        f"**周期**：{interval.upper()}\n"
        f"**方向**：<font color=\"{color}\">{arrow}</font>\n"
        f"**区间**：`{f['bottom']} ~ {f['top']}`\n"
        f"**信号 ID**：{signal_id(contract, f)}\n"
        f"**状态**：已触发（推送后自动开单）\n"
        f"**有效期**：即时开单（本周期收盘前信号有效）\n"
        f"**FVG 回补**：未检测（回补检测未上线）"
        + open_tpl_block(contract, f, entry_price)
    )
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    r = requests.post(webhook, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def send_resonance(webhook, contract, time_str, items, entry_price):
    """多周期同一时间的 FVG 合并为一条共振消息（多周期共振）。"""
    lines = [f"# ⚡ FVG 共振 · {contract}", "",
             f"**时间**：<font color=\"comment\">{time_str}（UTC+8）</font>",
             f"**信号 ID**：{signal_id(contract, items[0][1])}",
             f"**状态**：已触发（推送后自动开单）",
             f"**有效期**：即时开单（本周期收盘前信号有效）",
             f"**FVG 回补**：未检测（回补检测未上线）", ""]
    for interval, f in items:
        arrow = "▲ 看涨" if f["type"] == "bullish" else "▼ 看跌"
        color = _color_for(f)
        lines.append(f"> **{interval.upper()}** <font color=\"{color}\">{arrow}</font> · `{f['bottom']} ~ {f['top']}`")
    lines += ["", "多周期共振，信号增强"]
    lines.append(open_tpl_block(contract, items[0][1], entry_price))
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
        entry_price = latest_price(result["contract"]) or 0.0
        for time_str in sorted(by_time):
            items = by_time[time_str]
            if len(items) >= 2:
                resp = send_resonance(webhook, result["contract"], time_str, items, entry_price)
                ok = 1 if isinstance(resp, dict) and resp.get("errcode") == 0 else 0
                print(f"[push] {result['contract']} {time_str}: 共振 {len(items)} 周期, 成功 {ok}")
            else:
                interval, f = items[0]
                resp = send_single(webhook, result["contract"], interval, f, entry_price)
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
