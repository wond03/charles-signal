#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FVG 自动交易 · 每日统计与推送（完整骨架版）
=============================================
按老板要求的日报骨架输出：
  一、基础信息（口径）   二、账户概览   三、交易概览   四、盈亏拆解
  五、绩效指标           六、平仓明细   七、持仓/挂单   八、FVG 信号
  九、系统运行           十、总结与计划

数据源与口径（重要，避免数字对不上）：
  - 开单数：daily_trades_<date>.json 中 ordId 去重（trader.py 每次开单真实记录）
  - 逐笔成交：OKX /api/v5/trade/fills（demo 环境唯一可用成交接口，可能不全；
    同一订单多次部分成交会拆成多条，故明细条数 >= 开单数）
  - 账户权益：/api/v5/account/balance；持仓：/api/v5/account/positions
  - 挂单：/api/v5/trade/orders-algo-pending（OCO）
  - 净盈亏口径：已实现盈亏(pnl) + 手续费(fee)；资金费、未实现浮盈单独列出并标注

用法:
  python3 daily_report.py --date 2026-09-23               # 完整版输出
  python3 daily_report.py --date 2026-09-23 --push-cli    # 完整版+直发个人
  python3 daily_report.py --date 2026-09-23 --push-cli --compact  # 精简推送版
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import okx_exec

BJ_TZ = timezone(timedelta(hours=8))
WECOM_USERID = "won22_bgAAVzd-4_LKT6VxA_Sf8lNoIw"  # Wind
WECOM_CLI = (os.environ.get("WECOM_CLI")
             or shutil.which("wecom-cli")
             or "/home/marvis/.npm-global/bin/wecom-cli")

_INST_OKX = {"BTC_USDT": "BTC-USDT-SWAP", "XAU_USDT": "XAU-USDT-SWAP"}
_STRATEGY = "FVG 三K线缺口策略"
_PARAMS = "gap>=1.0 | 窗口 08:00~次日05:00(北京时间) | BTC 100x / XAU 50x | 单笔5U | RR 1:2"


def day_range(day_str):
    """返回北京时间某自然日的 (start_bj, end_bj)。"""
    day = datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=BJ_TZ)
    return day, day + timedelta(days=1)


def to_ms(ts_bj):
    return int(ts_bj.timestamp() * 1000)


def _safe(fn, default=None):
    """统一防御：接口失败返回 default 并打印原因。"""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return {"__error__": f"{type(e).__name__}: {e}"}


# ---------------- 数据采集 ----------------

def load_daily_trades(day_str, base_dir="."):
    """trader.py 记录的开仓明细（真实开单数来源）。"""
    path = os.path.join(base_dir, f"daily_trades_{day_str}.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            rows = json.load(f)
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []


def load_fvg_pushed(day_str, base_dir="."):
    """当日推送过的 FVG 信号 key（contract|interval|time）。"""
    path = os.path.join(base_dir, ".fvg_pushed.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return []
    keys = [k for k in d.keys() if day_str in str(k)]
    return sorted(keys)


def fetch_fills(contract, begin_ms, end_ms):
    """拉 OKX 当日成交（demo fills 可用），本地按时间过滤，返回升序列表。"""
    inst_okx = _INST_OKX.get(contract, contract)
    data = okx_exec._private_request("GET", "/api/v5/trade/fills",
                                     params={"instType": "SWAP", "instId": inst_okx, "limit": "100"})
    out = []
    for o in data.get("data", []):
        try:
            ts = int(o.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if begin_ms <= ts < end_ms:
            out.append(o)
    out.sort(key=lambda x: int(x.get("ts") or 0))
    return out


def fetch_account_snapshot():
    """账户概览：权益/可用/占用。返回 dict 或错误标记。"""
    data = okx_exec._private_request("GET", "/api/v5/account/balance")
    details = data.get("data", [{}])[0].get("details", [])
    for d in details:
        if d.get("ccy") == "USDT":
            return {
                "totalEq": float(d.get("eq") or 0.0),
                "availEq": float(d.get("availEq") or 0.0),
                "availBal": float(d.get("availBal") or 0.0),
                "frozenBal": float(d.get("frozenBal") or 0.0),
                "upl": float(d.get("upl") or 0.0),
                "uplRatio": float(d.get("uplRatio") or 0.0),
            }
    return {}


def fetch_positions():
    """当前持仓列表（demo positions 可用）。"""
    return okx_exec._private_request("GET", "/api/v5/account/positions", params={}).get("data", [])


def fetch_algo_pending():
    """OCO 挂单（orders-algo-pending）。"""
    return okx_exec._private_request("GET", "/api/v5/trade/orders-algo-pending",
                                     params={"ordType": "oco"}).get("data", [])


def fetch_github_runs():
    """最近 GitHub Actions 运行状态（仅 Actions 内 GITHUB_TOKEN 可用）。"""
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        return None
    import urllib.request
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/runs?per_page=6",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode())
        return [(x.get("conclusion") or x.get("status") or "?",
                 x.get("created_at", "")[:16].replace("T", " ")) for x in d.get("workflow_runs", [])]
    except Exception:  # noqa: BLE001
        return None


# ---------------- 统计计算 ----------------

def _fmt_px(px):
    """价格展示：数值转 float 保留 2 位；异常原样返回。"""
    if px in (None, "-", ""):
        return "-"
    try:
        return f"{float(px):.2f}"
    except (TypeError, ValueError):
        return str(px)


def _aggregate_closed(fills):
    """把逐笔成交按 ordId 聚合为订单级记录。"""
    by_oid = {}
    for o in fills:
        oid = o.get("ordId") or "?"
        by_oid.setdefault(oid, []).append(o)
    closed = []
    for oid, items in by_oid.items():
        pnl = sum(float(o.get("pnl") or 0.0) for o in items)
        fee = sum(float(o.get("fee") or 0.0) for o in items)
        side = items[0].get("side") or "?"
        px = items[0].get("avgPx") or items[0].get("px")
        ts = items[0].get("ts")
        closed.append({"ord_id": oid, "side": side, "px": px, "ts": ts,
                       "pnl": pnl, "fee": fee, "net": pnl + fee})
    return closed


def _fmt_ts(ts_ms):
    if not ts_ms:
        return "-"
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000, BJ_TZ).strftime("%H:%M")
    except (TypeError, ValueError, OSError):
        return "-"


def compute_metrics(closed):
    """绩效指标：基于平仓订单（pnl 已实现）。样本不足时标注。"""
    if not closed:
        return {"sample": False}
    wins = [c for c in closed if c["pnl"] > 0]
    losses = [c for c in closed if c["pnl"] < 0]
    flat = [c for c in closed if c["pnl"] == 0]
    n = len(closed)
    metrics = {"sample": True, "n": n}
    metrics["wins"] = len(wins)
    metrics["losses"] = len(losses)
    metrics["flat"] = len(flat)
    metrics["win_rate"] = len(wins) / n if n else 0.0
    avg_win = (sum(c["pnl"] for c in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(c["pnl"] for c in losses) / len(losses)) if losses else 0.0
    metrics["avg_win"] = avg_win
    metrics["avg_loss"] = avg_loss
    metrics["profit_factor"] = (sum(c["pnl"] for c in wins) / abs(sum(c["pnl"] for c in losses))
                                if losses and sum(c["pnl"] for c in losses) != 0 else None)
    metrics["expectancy"] = (sum(c["net"] for c in closed) / n) if n else 0.0
    metrics["max_win"] = max(c["pnl"] for c in closed) if n else 0.0
    metrics["max_loss"] = min(c["pnl"] for c in closed) if n else 0.0
    # 连续盈/亏
    streak_w, streak_l, cur_w, cur_l = 0, 0, 0, 0
    for c in sorted(closed, key=lambda x: x["ts"] or 0):
        if c["pnl"] > 0:
            cur_w += 1
            cur_l = 0
        elif c["pnl"] < 0:
            cur_l += 1
            cur_w = 0
        streak_w = max(streak_w, cur_w)
        streak_l = max(streak_l, cur_l)
    metrics["max_streak_win"] = streak_w
    metrics["max_streak_loss"] = streak_l
    # 累计盈亏曲线 -> 最大回撤
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for c in sorted(closed, key=lambda x: x["ts"] or 0):
        cum += c["net"]
        peak = max(peak, cum)
        if peak > 0:
            max_dd = max(max_dd, (peak - cum) / peak)
    metrics["max_drawdown"] = max_dd
    return metrics


# ---------------- 日报生成 ----------------

def build_report(day_str, dry=False, base_dir=".", compact=False):
    """生成日报。compact=True 返回企微可推送的精简版（<2048B）。"""
    start_bj, end_bj = day_range(day_str)
    begin_ms, end_ms = to_ms(start_bj), to_ms(end_bj)
    lines = []
    lines.append(f"📊 FVG 自动交易日报 · {day_str}")
    lines.append("")

    # ---- 采集（全部防御式） ----
    trades = load_daily_trades(day_str, base_dir)
    fvg_keys = load_fvg_pushed(day_str, base_dir)
    fills_all = []
    for contract in _INST_OKX:
        r = _safe(lambda c=contract: fetch_fills(c, begin_ms, end_ms), None)
        if isinstance(r, list):
            fills_all.extend(r)
        else:
            lines.append(f"⚠️ fills 查询失败 {contract}: {r}")
    fills_all.sort(key=lambda x: int(x.get("ts") or 0))
    acct = _safe(fetch_account_snapshot, None)
    pos = _safe(fetch_positions, None)
    algo = _safe(fetch_algo_pending, None)
    gh_runs = _safe(fetch_github_runs, None)

    # ---- 计算 ----
    open_oids = {t.get("ord_id") for t in trades if t.get("ord_id")}
    n_open = len(open_oids)
    closed = _aggregate_closed(fills_all)
    # 平仓订单 = 已产生已实现盈亏(pnl≠0)的订单；手续费为全账户当日所有成交支出
    closed_traded = [c for c in closed if c["pnl"] != 0]
    realized = sum(c["pnl"] for c in closed)
    fees = sum(float(o.get("fee") or 0.0) for o in fills_all)
    net = realized + fees
    upl = 0.0
    n_pos = 0
    if isinstance(pos, list):
        for p in pos:
            try:
                if float(p.get("pos") or 0) != 0:
                    n_pos += 1
                    upl += float(p.get("upl") or 0.0)
            except (TypeError, ValueError):
                pass
    n_algo = len(algo) if isinstance(algo, list) else -1
    pos_by_contract = {}
    dir_stat = {"buy": 0, "sell": 0}
    period_stat = {}
    for t in trades:
        dir_stat[t.get("side") or "?"] = dir_stat.get(t.get("side") or "?", 0) + 1
        pos_by_contract[t.get("contract")] = pos_by_contract.get(t.get("contract"), 0) + 1
        period_stat[t.get("interval")] = period_stat.get(t.get("interval"), 0) + 1
    fvg_stat = {}
    for k in fvg_keys:
        parts = k.split("|")
        c = parts[0].replace("_USDT", "") if len(parts) > 0 else "?"
        iv = parts[1] if len(parts) > 1 else "?"
        fvg_stat[(c, iv)] = fvg_stat.get((c, iv), 0) + 1
    metrics = compute_metrics(closed_traded)

    # ============ 一、基础信息（口径） ============
    lines.append("**一、基础信息（口径）**")
    lines.append(f"- 统计周期：{day_str} 00:00~23:59（北京时间 UTC+8）")
    lines.append("- 账户类型：OKX Demo 模拟盘（x-simulated-trading）")
    lines.append("- 交易所/数据源：OKX SWAP 成交 · Gate.io 行情（FVG 扫描）")
    lines.append(f"- 策略：{_STRATEGY}")
    lines.append(f"- 参数：{_PARAMS}")
    lines.append(f"- 开单数口径：daily_trades 中 ordId 去重 = {n_open} 单")
    lines.append(f"- 明细条数口径：fills 逐笔成交 = {len(fills_all)} 条"
                 + ("（含部分成交拆分，故可多于开单数）" if len(fills_all) > n_open else ""))
    lines.append(f"- 平仓数口径：fills 中 pnl≠0 的订单 = {len([c for c in closed if c['pnl'] != 0])} 单")
    lines.append("- 净盈亏口径：已实现盈亏(pnl) + 手续费(fee)，资金费/未实现浮盈单独列出")
    lines.append("")

    # ============ 二、账户概览 ============
    lines.append("**二、账户概览**")
    if isinstance(acct, dict) and not acct.get("__error__") and acct:
        init_approx = acct["totalEq"] - net - upl  # 近似：昨日末权益
        ret = (net + upl) / init_approx if init_approx else 0.0
        lines.append(f"- 当前权益：{acct['totalEq']:.2f} USDT")
        lines.append(f"- 可用保证金：{acct['availEq']:.2f} USDT")
        lines.append(f"- 占用保证金：{acct['frozenBal']:.2f} USDT")
        lines.append(f"- 未实现浮盈：{acct['upl']:+.2f} USDT")
        lines.append(f"- 当日收益率（含浮盈）：{ret * 100:+.2f}%")
        lines.append("- 初始权益：无历史快照，按 当前权益-净盈亏-浮盈 反推")
    else:
        lines.append(f"- 接口不可用：{acct if acct else 'balance 无返回'}")
    lines.append("")

    # ============ 三、交易概览 ============
    lines.append("**三、交易概览**")
    lines.append(f"- 开仓数：{n_open} 单（trader 记录）")
    lines.append(f"- 平仓数：{len([c for c in closed if c['pnl'] != 0])} 单（已实现盈亏）")
    lines.append(f"- 当前持仓：{n_pos} 个仓位"
                 + ("" if isinstance(pos, list) else "（positions 接口不可用）"))
    lines.append(f"- 挂单数（OCO）：{n_algo} 单" if n_algo >= 0 else "- 挂单数：查询失败")
    lines.append(f"- 多/空：多 {dir_stat.get('buy', 0)} / 空 {dir_stat.get('sell', 0)}")
    if pos_by_contract:
        lines.append("- 分品种：" + " / ".join(f"{k.replace('_USDT', '')} {v}单" for k, v in pos_by_contract.items()))
    if period_stat:
        lines.append("- 分周期：" + " / ".join(f"{k} {v}单" for k, v in sorted(period_stat.items(), key=lambda x: x[0])))
    lines.append("")

    # ============ 四、盈亏拆解 ============
    lines.append("**四、盈亏拆解**")
    lines.append(f"- 已实现盈亏：{realized:+.2f} USDT")
    lines.append(f"- 手续费：{fees:+.2f} USDT（负值=支出）")
    lines.append(f"- 资金费：接口不可用/未计入" )
    lines.append(f"- 未实现浮盈：{upl:+.2f} USDT（持仓 {n_pos} 个）")
    lines.append(f"- **净盈亏：{net:+.2f} USDT**（已实现+手续费，不含浮盈）")
    if realized == 0 and fees != 0:
        lines.append("  → 当日无平仓盈亏，净值为手续费支出")
    lines.append("")

    # ============ 五、绩效指标 ============
    lines.append("**五、绩效指标**")
    if metrics.get("sample"):
        m = metrics
        lines.append(f"- 胜率：{m['wins']}/{m['n']}（{m['win_rate'] * 100:.1f}%）")
        lines.append(f"- 平均盈利：{m['avg_win']:+.3f} / 平均亏损：{m['avg_loss']:+.3f}")
        lines.append(f"- 盈亏比：{m['profit_factor']:.2f}" if m["profit_factor"] else "- 盈亏比：暂无盈利/亏损")
        lines.append(f"- 期望值：{m['expectancy']:+.3f} USDT/单")
        lines.append(f"- 最大单笔盈/亏：{m['max_win']:+.2f} / {m['max_loss']:+.2f}")
        lines.append(f"- 最大回撤：{m['max_drawdown'] * 100:.1f}%")
        lines.append(f"- 连续盈/亏：{m['max_streak_win']} / {m['max_streak_loss']}")
        lines.append("- 平均持仓时长：无法关联开/平仓（demo fills 限制）")
        lines.append("- R 倍数分布：需开平仓配对，当日不可用")
    else:
        lines.append("- 当日无平仓成交，绩效样本不足（待 OCO 触发后统计）")
    lines.append("")

    # ============ 六、平仓明细 ============
    lines.append("**六、平仓明细**")
    if closed_traded:
        for c in closed_traded[-10:]:
            arrow = "▲" if c["side"] == "buy" else "▼"
            lines.append(f"- {arrow} ordId {c['ord_id'][-6:]} {_fmt_ts(c['ts'])} "
                         f"价 {c['px'] or '-'} 费 {c['fee']:+.2f} 净 {c['net']:+.2f}")
        lines.append("  （字段限制：demo fills 无信号ID/止损止盈/开仓价，需配对 daily_trades）")
    else:
        lines.append("- 当日无平仓成交记录（demo fills 未返回平仓单，或 OCO 尚未触发）")
    lines.append("")

    # ============ 七、持仓/挂单 ============
    lines.append("**七、持仓/挂单**")
    if isinstance(pos, list) and pos:
        for p in pos:
            try:
                if float(p.get("pos") or 0) == 0:
                    continue
            except (TypeError, ValueError):
                continue
            inst = p.get("instId", "?")
            side = p.get("posSide", "?")
            avg = _fmt_px(p.get("avgPx"))
            mark = _fmt_px(p.get("markPx"))
            up = float(p.get("upl") or 0.0)
            lev = p.get("lev") or p.get("lever") or "-"
            lines.append(f"- {inst} {side} 开 {avg} 现 {mark} 浮盈 {up:+.2f} 杠杆 {lev}x")
        if not any(True for _ in pos):
            lines.append("- 无持仓")
    elif isinstance(pos, list):
        lines.append("- 无持仓")
    else:
        lines.append(f"- 持仓接口不可用：{pos}")
    if isinstance(algo, list):
        lines.append(f"- OCO 挂单：{len(algo)} 单")
        for a in algo[-5:]:
            lines.append(f"  - {a.get('instId')} {a.get('side')} 触发 {a.get('tpTriggerPx') or a.get('slTriggerPx') or '-'}")
    else:
        lines.append(f"- 挂单接口不可用：{algo}")
    lines.append("")

    # ============ 八、FVG 信号 ============
    lines.append("**八、FVG 信号**")
    lines.append(f"- 当日信号数：{len(fvg_keys)} 个（.fvg_pushed 口径，含已开单/未开单）")
    if fvg_stat:
        lines.append("- 分布：" + " / ".join(f"{c} {iv} {n}个" for (c, iv), n in sorted(fvg_stat.items())))
    lines.append("- 周期：1m/5m/15m/1h；类型：看涨/看跌（key 未存类型，按开单记录统计）")
    lines.append(f"- 已开单：{n_open} 单 / 信号 {len(fvg_keys)} 个（非严格一一对应，"
                 f"存在同一信号多单或未推送开单）")
    lines.append("")

    # ============ 九、系统运行 ============
    lines.append("**九、系统运行**")
    lines.append(f"- 报告生成：{datetime.now(BJ_TZ).strftime('%Y-%m-%d %H:%M:%S')}（UTC+8）")
    t0 = time.time()
    ping = _safe(lambda: okx_exec.ping(), None)
    if isinstance(ping, dict):
        lines.append(f"- OKX API 延迟：{(time.time() - t0) * 1000:.0f} ms")
    else:
        lines.append(f"- OKX API 延迟：测量失败（{ping}）")
    lines.append("- 接口状态：balance "
                 + ("OK" if isinstance(acct, dict) and not acct.get("__error__") else f"FAIL({acct})")
                 + " / positions "
                 + ("OK" if isinstance(pos, list) else f"FAIL({pos})")
                 + " / algos "
                 + ("OK" if isinstance(algo, list) else f"FAIL({algo})"))
    if isinstance(gh_runs, list):
        ok = sum(1 for s, _ in gh_runs if s == "success")
        lines.append(f"- Actions 近{len(gh_runs)}次运行：成功 {ok} 次 / 失败 {len(gh_runs) - ok} 次")
        for s, t in gh_runs[:3]:
            lines.append(f"  - {t} {s}")
    else:
        lines.append("- Actions 运行状态：非 Actions 环境，未查询")
    lines.append("- 断线重连/拒单原因：脚本未记录，暂无")
    lines.append("")

    # ============ 十、总结与计划 ============
    lines.append("**十、总结与计划**")
    issues = []
    if n_open and not closed_traded:
        issues.append(f"开仓 {n_open} 单，当日尚无平仓成交（OCO 未触发或 demo fills 未返回平仓），盈亏未实现")
    if isinstance(pos, list) and n_pos:
        issues.append(f"仍有 {n_pos} 个持仓未平，注意风控")
    if isinstance(acct, dict) and acct.get("__error__"):
        issues.append("balance 接口失败，权益数据缺失")
    if not issues:
        issues.append("当日运行正常")
    lines.append("- 今日总结：" + "；".join(issues))
    lines.append("- 异常说明：见各节接口状态标注")
    lines.append("- 明日计划：保持每 5 分钟扫描开单，持续观察 OCO 平仓与绩效统计")
    lines.append("- 参数调整：无（如需调整杠杆/单笔/RR 请告知）")
    lines.append("- 风险提醒：模拟盘 5U/单，当前仅手续费消耗；实盘前务必核对持仓与接口")
    lines.append("")
    lines.append("> 模拟盘自动交易 · FVG 三K线缺口策略")

    text = "\n".join(lines)
    if compact:
        text = _compact_text(day_str, n_open, len(fills_all), net, realized, fees, upl, n_pos,
                             n_algo, metrics, pos_by_contract, fvg_keys, issues)
    if dry:
        text += "\n\n[DRY-RUN 未推送]"
    return text


def _compact_text(day_str, n_open, n_fills, net, realized, fees, upl, n_pos, n_algo,
                  metrics, pos_by_contract, fvg_keys, issues):
    """企微推送精简版（text 上限 2048B），保留五块核心：口径/持仓/绩效/风险/系统运行。"""
    lines = []
    lines.append(f"📊 FVG 日报 · {day_str}（UTC+8）")
    lines.append(f"账户：OKX Demo | 开单 {n_open}（trader口径）/ 成交 {n_fills} 条")
    lines.append(f"持仓 {n_pos} | OCO挂单 {n_algo} | 信号 {len(fvg_keys)} 个")
    if pos_by_contract:
        lines.append("分品种：" + " / ".join(f"{k.replace('_USDT', '')} {v}单" for k, v in pos_by_contract.items()))
    lines.append(f"已实现 {realized:+.2f} | 手续费 {fees:+.2f} | 浮盈 {upl:+.2f}")
    lines.append(f"**净盈亏 {net:+.2f} USDT**（不含浮盈）")
    if metrics.get("sample"):
        lines.append(f"绩效：胜率 {metrics['win_rate']*100:.0f}% | 期望 {metrics['expectancy']:+.3f}/单 | 回撤 {metrics['max_drawdown']*100:.1f}%")
    else:
        lines.append("绩效：无平仓，样本不足")
    if issues:
        lines.append("注意：" + "；".join(issues))
    lines.append("")
    lines.append("> 完整版见仓库 daily_report_*.md")
    return "\n".join(lines)


# ---------------- 推送 ----------------

def push_webhook(text, webhook):
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
    parser = argparse.ArgumentParser(description="FVG 自动交易每日统计（完整骨架）")
    parser.add_argument("--date", default=None, help="日期 YYYY-MM-DD（北京时间），默认今天")
    parser.add_argument("--push-webhook", action="store_true", help="推送到企业微信群机器人")
    parser.add_argument("--webhook", default=None, help="群机器人 webhook（覆盖配置）")
    parser.add_argument("--push-cli", action="store_true", help="通过 wecom-cli 直发授权真人")
    parser.add_argument("--compact", action="store_true", help="推送精简版（控制2048B内）")
    parser.add_argument("--save-dir", default=None, help="保存完整日报 daily_report_<date>.md")
    parser.add_argument("--base-dir", default=".", help="daily_trades/.fvg_pushed 所在目录")
    args = parser.parse_args()

    day_str = args.date or datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    full = build_report(day_str, base_dir=args.base_dir, compact=False)
    push = build_report(day_str, base_dir=args.base_dir, compact=True)

    print(full)

    if args.save_dir:
        os.makedirs(args.save_dir, exist_ok=True)
        out_path = os.path.join(args.save_dir, f"daily_report_{day_str}.md")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(full)
        print(f"[saved] {out_path}")

    sent = False
    if args.push_webhook:
        wh = load_webhook(args.webhook)
        if not wh:
            print("未找到 webhook", file=sys.stderr)
            sys.exit(2)
        resp = push_webhook(push, wh)
        print("[push-webhook]", resp)
        sent = True
    if args.push_cli:
        out = push_cli(push if args.compact else full)
        print("[push-cli]", out[:200])
        sent = True
    if not sent:
        print("[info] 未推送，仅输出。加 --push-webhook 或 --push-cli 推送。")


if __name__ == "__main__":
    main()
