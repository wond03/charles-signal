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


def rolling_window():
    """返回滚动 24h 窗口 (start_bj, end_bj)：从当前推送时刻往前推 24h。
    例如 4:20 推送 -> 前日 4:20 ~ 今日 4:20。"""
    end_bj = datetime.now(BJ_TZ)
    return end_bj - timedelta(hours=24), end_bj


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


def load_daily_trades_window(win_start, win_end, base_dir="."):
    """滚动窗口内开仓明细：跨日合并 daily_trades_*.json，按 time 过滤 [win_start, win_end)。"""
    trades = []
    day = win_start.date()
    while day <= win_end.date():
        trades.extend(load_daily_trades(day.strftime("%Y-%m-%d"), base_dir))
        day += timedelta(days=1)
    out = []
    for t in trades:
        ts = t.get("time")
        if not ts:
            continue
        try:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=BJ_TZ)
        except ValueError:
            continue
        if win_start <= dt < win_end:
            out.append(t)
    return out


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


def load_fvg_pushed_window(win_start, win_end, base_dir="."):
    """滚动窗口内推送过的 FVG 信号 key：按 key 中时间字段过滤 [win_start, win_end)。"""
    path = os.path.join(base_dir, ".fvg_pushed.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return []
    keys = []
    for k in d.keys():
        parts = str(k).split("|")
        if len(parts) < 3:
            continue
        try:
            t = datetime.strptime(parts[2], "%Y-%m-%d %H:%M").replace(tzinfo=BJ_TZ)
        except ValueError:
            continue
        if win_start <= t < win_end:
            keys.append(k)
    return sorted(keys)


def fetch_fills(contract, begin_ms, end_ms):
    """拉 OKX 当日成交（demo fills 可用），本地按时间过滤，返回升序列表。
    注意：fills 的 pnl 字段恒为 0，真实已实现盈亏需从 bills(type=2) 获取。"""
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


def fetch_bills(contract, begin_ms, end_ms, max_pages=10, bill_type="2"):
    """拉 OKX 账单（/api/v5/account/bills），返回升序列表。
    bill_type=2 平仓记录：真实已实现盈亏在 pnl 字段、手续费在 fee 字段；
    bill_type=8 资金费记录：金额（含符号）在 pnl 字段。
    按 begin/end 窗口分页拉取。
    同一订单多次成交会拆成多条 bill（ordId 相同），拉全后由
    _aggregate_closed_from_bills 合并为订单级，避免平仓笔数虚高。"""
    inst_okx = _INST_OKX.get(contract, contract)
    out, after = [], ""
    rows = []
    for _ in range(max_pages):
        params = {"instType": "SWAP", "instId": inst_okx,
                  "begin": str(begin_ms), "end": str(end_ms),
                  "type": bill_type, "limit": "100"}
        if after:
            params["after"] = after
        data = okx_exec._private_request("GET", "/api/v5/account/bills", params=params)
        rows = data.get("data", [])
        for o in rows:
            try:
                ts = int(o.get("ts") or 0)
            except (TypeError, ValueError):
                continue
            if begin_ms <= ts < end_ms:
                out.append(o)
        if len(rows) < 100:
            break
        after = rows[-1].get("billId", "")
        if not after:
            break
    if len(rows) >= 100 and after:
        print(f"[warn] fetch_bills {contract}: 已达 {max_pages} 页分页上限，账单可能截断", flush=True)
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


def collect_snapshot():
    """单次采集账户快照 (acct, pos, algo)，供 build_report 复用，消除双快照浮盈跳变。"""
    return (_safe(fetch_account_snapshot, None),
            _safe(fetch_positions, None),
            _safe(fetch_algo_pending, None))


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


# 平仓原因判定容差（相对价差 0.15%，兼顾滑点与窄缺口场景）
_TP_SL_TOL = 0.0015
# OKX bills subType 强平/强减枚举（type=2 平仓类：16/17/24/25/32/33=强平，13/14=强减，207/209=强平）
_LIQ_SUBTYPES = {"13", "14", "16", "17", "24", "25", "32", "33", "207", "209"}


def _classify_close_reason(rec):
    """平仓原因分类：tp=止盈 / sl=止损(含保本) / liq=强平 / manual=手动平仓。
    手动平仓（老板或 Agent 手动执行）不计入绩效统计，仅在报告中备注。"""
    sub = str(rec.get("sub_type") or "")
    if sub in _LIQ_SUBTYPES:
        return "liq"
    try:
        px = float(rec.get("px"))
    except (TypeError, ValueError):
        return "manual"  # 无成交价无法判定，按手动处理
    if px <= 0:
        return "manual"

    def _near(a, b):
        try:
            return abs(float(a) - float(b)) / abs(float(b)) <= _TP_SL_TOL
        except (TypeError, ValueError, ZeroDivisionError):
            return False

    # 优先级：止盈价 > 止损价 > 保本位（价格≈开仓价，系统自动移保本，归止损侧分母）
    if _near(rec.get("tp"), px):
        return "tp"
    if _near(rec.get("sl"), px):
        return "sl"
    if _near(rec.get("entry"), px):
        return "sl"
    return "manual"


def _aggregate_closed_from_bills(bills, trades):
    """基于 bills(type=2 平仓) 聚合为订单级记录（真实 pnl/fee）。
    同一订单多次成交会被拆成多条 bill（ordId 相同），先按 ordId 合并为一条订单记录，
    再按 instId+方向+时间最近匹配回开仓单，补齐 interval/direction 供分周期×方向统计。
    匹配失败的保留为平仓记录。"""
    # ---- 同一订单多次成交合并（ordId 相同 → 一笔；pnl/fee/sz 求和，ts 取最早）----
    merged = {}
    order_seq = []
    for b in bills:
        oid = b.get("ordId")
        if oid is None:
            # 无 ordId 无法合并，按单条处理
            merged.setdefault(("_raw", id(b)), dict(b))
            order_seq.append(("_raw", id(b)))
            continue
        oid = str(oid)
        if oid not in merged:
            merged[oid] = dict(b)
            order_seq.append(oid)
        else:
            m = merged[oid]
            def _num(v):
                try:
                    return float(v or 0.0)
                except (TypeError, ValueError):
                    return 0.0
            m["pnl"] = _num(m.get("pnl")) + _num(b.get("pnl"))
            m["fee"] = _num(m.get("fee")) + _num(b.get("fee"))
            m["sz"] = _num(m.get("sz")) + _num(b.get("sz"))
            if _num(b.get("ts")) < _num(m.get("ts")):
                m["ts"] = b.get("ts")
            # subType / side / instId 同单一致，保留首条
    bills = [merged[k] for k in order_seq]
    # 开仓单按合约+方向分组，各维护按时间排序的待匹配队列（含剩余可匹配数量）
    trade_queues = {}
    for t in trades:
        if not t.get("ord_id"):
            continue
        key = (t.get("contract"), t.get("direction") or t.get("side"))
        try:
            qty = float(t.get("qty") or 0.0)
        except (TypeError, ValueError):
            qty = 0.0
        trade_queues.setdefault(key, []).append([t, qty])  # [trade, remaining_qty]
    for q in trade_queues.values():
        q.sort(key=lambda x: x[0].get("time") or "")
    inst_contract = {v: k for k, v in _INST_OKX.items()}
    closed = []
    for b in bills:
        inst = b.get("instId", "")
        contract = inst_contract.get(inst, inst)
        sub = str(b.get("subType") or "")
        # subType 语义（OKX demo 实测，type=2 含开平仓）：
        #   3=开多 4=开空 5=平多 6=平空；200+ 为强平/减仓（方向看 side）
        # 3/4 是开仓记录（pnl=0），不算平仓，直接跳过
        if sub in ("3", "4"):
            continue
        if sub == "5":
            direction = "long"
        elif sub == "6":
            direction = "short"
        else:
            # 强平/减仓等异常子类型：买入=平空(short)，卖出=平多(long)
            direction = "short" if str(b.get("side") or "").lower() == "buy" else "long"
        try:
            ts = int(b.get("ts") or 0)
        except (TypeError, ValueError):
            ts = 0
        try:
            sz = float(b.get("sz") or 0.0)
        except (TypeError, ValueError):
            sz = 0.0
        # FIFO 匹配：同合约同方向中最早开仓（且已到平仓时间）的未消耗完开仓单
        matched = None
        for item in trade_queues.get((contract, direction), []):
            t, remaining = item
            if remaining <= 0:
                continue
            try:
                open_dt = datetime.strptime(t["time"], "%Y-%m-%d %H:%M").replace(tzinfo=BJ_TZ)
                open_ms = int(open_dt.timestamp() * 1000)
            except (ValueError, KeyError, TypeError):
                continue
            if open_ms <= ts:
                matched = item
                break
        side = "sell" if direction == "short" else "buy"
        pnl = float(b.get("pnl") or 0.0)
        fee = float(b.get("fee") or 0.0)
        rec = {"ord_id": b.get("ordId") or "?",
               "side": side, "direction": direction,
               "interval": (matched[0] if matched else {}).get("interval") or "?",
               "contract": contract,
               "px": b.get("px"), "ts": b.get("ts"),
               "pnl": pnl, "fee": fee, "net": pnl + fee,
               "sub_type": sub,
               "tp": (matched[0] if matched else {}).get("tp"),
               "sl": (matched[0] if matched else {}).get("sl"),
               "entry": (matched[0] if matched else {}).get("entry_ref")}
        if matched:
            # 按平仓数量消耗开仓单剩余量；无 sz 时退化为一次性消耗
            if sz > 0:
                matched[1] -= sz
            else:
                matched[1] = 0.0
            rec["ord_id"] = matched[0]["ord_id"]
            rec["open_time"] = matched[0].get("time")
        rec["reason"] = _classify_close_reason(rec)
        closed.append(rec)
    closed.sort(key=lambda x: int(x.get("ts") or 0))
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

def build_report(day_str, dry=False, base_dir=".", compact=False, rolling=False, snapshot=None):
    """生成日报。compact=True 返回企微推送版（模板结构，<2048B）。
    rolling=True 时按当前推送时刻往前推 24h 的滚动窗口统计；否则按 day_str 自然日。
    snapshot 为 (acct, pos, algo) 元组时复用外部采集的快照，避免多次调用拉取不一致。"""
    if rolling:
        start_bj, end_bj = rolling_window()
    else:
        start_bj, end_bj = day_range(day_str)
    begin_ms, end_ms = to_ms(start_bj), to_ms(end_bj)
    warn = []

    # ---- 采集（全部防御式） ----
    if rolling:
        trades = load_daily_trades_window(start_bj, end_bj, base_dir)
        fvg_keys = load_fvg_pushed_window(start_bj, end_bj, base_dir)
    else:
        trades = load_daily_trades(day_str, base_dir)
        fvg_keys = load_fvg_pushed(day_str, base_dir)
    # 匹配池扩展：bills 平仓窗口内可能平掉窗口更早开仓的单子（如 24H 滚动窗口外的老仓），
    # 匹配池向前扩 3 天，避免老仓平仓记录落入 interval="?" 导致分周期/共振统计失真。
    trades_pool = load_daily_trades_window(start_bj - timedelta(days=3), end_bj, base_dir)
    fills_all = []
    bills_all = []
    for contract in _INST_OKX:
        r = _safe(lambda c=contract: fetch_bills(c, begin_ms, end_ms), None)
        if isinstance(r, list):
            bills_all.extend(r)
        else:
            warn.append(f"bills {contract} 查询失败")
        f = _safe(lambda c=contract: fetch_fills(c, begin_ms, end_ms), None)
        if isinstance(f, list):
            fills_all.extend(f)
        else:
            warn.append(f"fills {contract} 查询失败")
    fills_all.sort(key=lambda x: int(x.get("ts") or 0))
    # 资金费（bills type=8，窗口内真实已结算金额；pnl 字段含符号）
    funding_total = 0.0
    funding_ok = True
    for contract in _INST_OKX:
        fb = _safe(lambda c=contract: fetch_bills(c, begin_ms, end_ms, bill_type="8"), None)
        if isinstance(fb, list):
            funding_total += sum(float(b.get("pnl") or 0.0) for b in fb)
        else:
            funding_ok = False
            warn.append(f"funding bills {contract} 查询失败")
    funding_text = f"{funding_total:+.2f}" if funding_ok else "查询失败"
    if snapshot is not None:
        acct, pos, algo = snapshot
    else:
        acct = _safe(fetch_account_snapshot, None)
        pos = _safe(fetch_positions, None)
        algo = _safe(fetch_algo_pending, None)
    gh_runs = _safe(fetch_github_runs, None)

    # ---- 基础计算 ----
    open_oids = {t.get("ord_id") for t in trades if t.get("ord_id")}
    n_open = len(open_oids)
    closed = _aggregate_closed_from_bills(bills_all, trades_pool)
    closed_traded = [c for c in closed if c["pnl"] != 0]
    realized = sum(c["pnl"] for c in closed)
    # 手续费统一取 bills（含开平仓全部成交，与 realized 同源，避免 fills 分页不全）
    fees = sum(float(o.get("fee") or 0.0) for o in bills_all)
    net = realized + fees
    upl = 0.0
    n_pos = 0
    pos_rows_raw = []
    if isinstance(pos, list):
        for p in pos:
            try:
                if float(p.get("pos") or 0) == 0:
                    continue
            except (TypeError, ValueError):
                continue
            n_pos += 1
            upl += float(p.get("upl") or 0.0)
            pos_rows_raw.append(p)
    dir_stat = {"buy": 0, "sell": 0}
    for t in trades:
        dir_stat[t.get("side") or "?"] = dir_stat.get(t.get("side") or "?", 0) + 1

    # ---- 手动平仓口径：reason=manual（老板/Agent 手动执行）不计入绩效统计，仅备注 ----
    closed_stat = [c for c in closed if c.get("reason") != "manual"]
    manual_closed = [c for c in closed if c.get("reason") == "manual"]
    manual_n = len(manual_closed)
    manual_pnl = sum(c["pnl"] for c in manual_closed)
    metrics = compute_metrics(closed_stat)

    # ---- 分周期×品种绩效（模板口径）：止盈/止损/强平分类，手动平仓剔除 ----
    period_perf = {}   # (iv, contract) -> {"tp": n, "sl": n, "liq": n, "pnl": realized}
    for c in closed_stat:
        iv = c.get("interval") or "?"
        key = (iv, c.get("contract") or "?")
        p = period_perf.setdefault(key, {"tp": 0, "sl": 0, "liq": 0, "pnl": 0.0})
        r = c.get("reason") or "manual"
        if r in ("tp", "sl", "liq"):
            p[r] += 1
        p["pnl"] += c["pnl"]

    # ---- 多周期共振：同一合约+方向+开单时刻(分钟级)聚合周期集合 ----
    # 避免按 (contract, direction) 全量聚合导致周期虚高（如全天 5m 单混入三周期）
    reso_groups = {}
    for t in trades:
        key = (t.get("contract"), t.get("direction") or t.get("side"), t.get("time"))
        g = reso_groups.setdefault(key, {"contract": t.get("contract"), "dir": key[1],
                                         "time": t.get("time"),
                                         "intervals": set(), "entries": [],
                                         "last_time": t.get("time"), "n": 0})
        g["intervals"].add(t.get("interval"))
        g["entries"].append(t.get("entry_ref"))
        g["n"] += 1
        if (t.get("time") or "") > (g["last_time"] or ""):
            g["last_time"] = t.get("time")
    # positions 方向匹配：instId 合约 + posSide long/short
    pos_map = {}
    for p in pos_rows_raw:
        inst = p.get("instId", "")
        side = p.get("posSide", "").lower()
        pos_map.setdefault((inst, side), []).append(p)
    inst_contract = {v: k for k, v in _INST_OKX.items()}
    reso_list = []
    for (contract, dirn, _tm), g in sorted(reso_groups.items()):
        if len(g["intervals"]) < 2:
            continue
        inst = _INST_OKX.get(contract, contract)
        pside = "long" if dirn in ("long", "buy") else "short"
        holding = pos_map.get((inst, pside), []) or pos_map.get((inst, dirn), [])
        if not holding:
            continue  # 共振口径：仅统计当前持仓中的多周期组合
        ivs = sorted(g["intervals"], key=lambda x: {"5m": 0, "15m": 1, "1h": 2, "4h": 3}.get(x, 9))
        combo = "+".join(iv.upper() for iv in ivs)
        is_triple = len(ivs) >= 3
        p = holding[0]
        avg = _fmt_px(p.get("avgPx"))
        mark = _fmt_px(p.get("markPx"))
        up = float(p.get("upl") or 0.0)
        reso_list.append({"contract": contract, "combo": combo, "dir": dirn,
                          "status": "持仓中", "open": avg, "cur": mark,
                          "pnl": up, "holding": True, "triple": is_triple})
    n_reso = len(reso_list)
    combo_stat = {}
    for r in reso_list:
        combo_stat[r["combo"]] = combo_stat.get(r["combo"], 0) + 1

    # ---- 共振组合平仓统计：closed(带 interval/direction/open_time) -> reso 组 -> (combo, contract) ----
    reso_closed_stats = {}
    for c in closed_stat:  # 手动平仓剔除
        if c.get("interval") == "?" or not c.get("open_time"):
            continue
        # 找包含该开仓单所属周期集合的共振组：合约+方向+开仓时刻一致
        key = (c.get("contract"), c.get("direction"), c.get("open_time"))
        g = reso_groups.get(key)
        if not g or len(g["intervals"]) < 2:
            continue
        ivs = sorted(g["intervals"], key=lambda x: {"5m": 0, "15m": 1, "1h": 2, "4h": 3}.get(x, 9))
        combo = "+".join(iv.upper() for iv in ivs)
        ckey = (combo, c.get("contract") or "?")
        st = reso_closed_stats.setdefault(ckey, {"n": 0, "tp": 0, "sl": 0, "liq": 0,
                                                 "realized": 0.0, "fee": 0.0})
        st["n"] += 1
        r = c.get("reason") or "manual"
        if r in ("tp", "sl", "liq"):
            st[r] += 1
        st["realized"] += c["pnl"]
        st["fee"] += c["fee"]

    # ---- 运行时长：今日最早信号/开单 -> 现在 ----
    run_hours = None
    t_min = None
    for t in trades:
        if t.get("time"):
            try:
                dt = datetime.strptime(t["time"], "%Y-%m-%d %H:%M").replace(tzinfo=BJ_TZ)
            except ValueError:
                continue
            if t_min is None or dt < t_min:
                t_min = dt
    if t_min is None:
        for k in fvg_keys:
            parts = k.split("|")
            if len(parts) >= 3:
                try:
                    dt = datetime.strptime(parts[2], "%Y-%m-%d %H:%M").replace(tzinfo=BJ_TZ)
                except ValueError:
                    continue
                if t_min is None or dt < t_min:
                    t_min = dt
    if t_min is not None:
        run_hours = (datetime.now(BJ_TZ) - t_min).total_seconds() / 3600.0

    # ---- 平均持仓时长（估算）：当前持仓最近开单 -> 现在 ----
    avg_hold_h = None
    if pos_rows_raw:
        hold_ts = []
        for p in pos_rows_raw:
            inst = p.get("instId", "")
            contract = inst_contract.get(inst)
            pside = p.get("posSide", "").lower()
            cand = [t for t in trades if t.get("contract") == contract
                    and (t.get("direction") or t.get("side")) == pside]
            if cand:
                try:
                    dt = datetime.strptime(cand[-1]["time"], "%Y-%m-%d %H:%M").replace(tzinfo=BJ_TZ)
                except (ValueError, KeyError):
                    continue
                hold_ts.append((datetime.now(BJ_TZ) - dt).total_seconds() / 3600.0)
        if hold_ts:
            avg_hold_h = sum(hold_ts) / len(hold_ts)

    # ================= 输出 =================
    if compact:
        # ===== 新版精简模板（老板指定）=====
        lines = []
        lines.append("📊 FVG 自动交易数据·最近24H")
        lines.append("━━━━━━━━━━━━")
        lines.append("")

        # 账户概览
        lines.append("账户概览")
        if isinstance(acct, dict) and acct and not acct.get("__error__"):
            init_approx = acct["totalEq"] - net - upl
            ret = (net + upl) / init_approx if init_approx else 0.0
            pf = metrics.get("profit_factor") if metrics.get("sample") else None
            pf_s = f"{pf:.2f}" if pf else "N/A"
            pct = ret * 100
            pct_s = f"{pct:+.3f}" if abs(pct) >= 0.0005 else "+0.000"
            manual_s = f"（手动 {manual_n} 笔另计）" if manual_n else ""
            lines.append(f"总盈亏 {net + upl:+.2f} | 净盈亏(已实现) {net:+.2f} | 总开仓 {n_open} | "
                         f"总平仓 {len(closed_stat)}{manual_s} | 收益率 {pct_s}% | 盈亏比 {pf_s}")
        else:
            lines.append(f"账户接口不可用：{acct if acct else 'balance 无返回'}")
        lines.append("")

        # 分周期×品种绩效（模板口径：止盈/止损/强平分类，盈亏=已实现不含手续费，手动平仓剔除）
        lines.append("分周期绩效（周期×品种）")
        iv_order = {"5m": "5M", "15m": "15M", "1h": "1H", "4h": "4H", "?": "老仓"}

        def _period_line(iv, contract):
            p = period_perf.get((iv, contract))
            if not p:
                return None
            n = p["tp"] + p["sl"] + p["liq"]
            if n == 0:
                return None
            wr = p["tp"] / n * 100
            ct = contract.replace("_USDT", "")
            return (f"{iv_order.get(iv, iv.upper())}·{ct} {n}单 | "
                    f"止盈{p['tp']}/止损{p['sl']}/强平{p['liq']} | "
                    f"{p['pnl']:+.2f}U | 胜率{wr:.1f}%")

        ivs = sorted({iv for (iv, _c) in period_perf},
                     key=lambda x: {"5m": 0, "15m": 1, "1h": 2, "4h": 3}.get(x, 9))
        if not ivs:
            lines.append("暂无平仓统计")
        for iv in ivs:
            for contract in ("BTC_USDT", "XAU_USDT"):
                line = _period_line(iv, contract)
                if line:
                    lines.append(line)
                else:
                    lines.append(f"{iv_order.get(iv, iv.upper())}·{contract.replace('_USDT', '')} —")
        lines.append("")

        # 多周期共振（组合×品种，模板口径）
        lines.append("多周期共振（组合×品种）")

        def _combo_order(combo):
            base = {"5M+15M": 0, "15M+1H": 1, "5M+1H": 2}.get(combo, 9)
            return (base, combo)

        reso_pos_stat = {}
        reso_pos_triple_n, reso_pos_triple_pnl = 0, 0.0
        for r in reso_list:
            if r.get("triple"):
                reso_pos_triple_n += 1
                reso_pos_triple_pnl += r["pnl"] or 0.0
            st = reso_pos_stat.setdefault((r["combo"], r["contract"]), {"n": 0, "pnl": 0.0})
            st["n"] += 1
            st["pnl"] += r["pnl"] or 0.0
        combos = sorted(set([k[0] for k in reso_closed_stats]) | set([k[0] for k in reso_pos_stat]),
                        key=_combo_order)
        if not combos:
            lines.append("暂无共振记录")
        for combo in combos:
            for contract in ("BTC_USDT", "XAU_USDT"):
                ct = contract.replace("_USDT", "")
                cst = reso_closed_stats.get((combo, contract), {"n": 0, "tp": 0, "sl": 0, "liq": 0,
                                                                "realized": 0.0, "fee": 0.0})
                pos_st = reso_pos_stat.get((combo, contract), {"n": 0, "pnl": 0.0})
                closed_n = cst["n"]
                pos_n = pos_st["n"]
                if closed_n == 0 and pos_n == 0:
                    continue
                if closed_n == 0:
                    # 仅持仓中：浮盈不参与胜负/胜率，避免“0单却有盈亏”的误读
                    lines.append(f"{combo}·{ct} 持仓中 {pos_n}笔 | 浮盈 {pos_st['pnl']:+.2f} | 止盈/止损/强平 -/-/- | 胜率 -")
                else:
                    wr = cst["tp"] / closed_n * 100
                    pos_txt = f" +持仓{pos_n}" if pos_n else ""
                    lines.append(f"{combo}·{ct} 已平{closed_n}{pos_txt}单 | "
                                 f"止盈{cst['tp']}/止损{cst['sl']}/强平{cst['liq']} | "
                                 f"盈亏 {cst['realized']:+.2f}U | 胜率{wr:.1f}%")
        # 三周期及以上汇总（intervals>=3，兼容 4H 参与的组合）
        cst = {"n": 0, "tp": 0, "sl": 0, "liq": 0, "realized": 0.0}
        for (combo, _c), st in reso_closed_stats.items():
            if len(combo.split("+")) >= 3:
                cst["n"] += st["n"]
                cst["tp"] += st["tp"]
                cst["sl"] += st["sl"]
                cst["liq"] += st["liq"]
                cst["realized"] += st["realized"]
        total_n = cst["n"] + reso_pos_triple_n
        if total_n:
            if cst["n"] == 0:
                lines.append(f"三周期 持仓中 {reso_pos_triple_n}笔 | 浮盈 {reso_pos_triple_pnl:+.2f}")
            else:
                wr = cst["tp"] / cst["n"] * 100
                pos_txt = f" +持仓{reso_pos_triple_n}" if reso_pos_triple_n else ""
                lines.append(f"三周期 已平{cst['n']}{pos_txt}单 | 止盈{cst['tp']}/止损{cst['sl']}/强平{cst['liq']} | "
                             f"盈亏 {cst['realized']:+.2f}U | 胜率{wr:.1f}%")
        lines.append("")

        # 盈亏拆解（账户口径：已实现含手动平仓；平台成交时已划扣手续费，盈亏不含手续费属正常）
        lines.append("盈亏拆解")
        lines.append(f"已实现：{realized:+.2f} | 未实现：{upl:+.2f}")
        lines.append(f"手续费：{fees:+.2f} | 资金费：{funding_text}")
        lines.append(f"净盈亏：{net:+.2f} USDT")
        if manual_n:
            lines.append(f"备注：手动平仓 {manual_n} 笔（盈亏 {manual_pnl:+.2f}U）仅备注，不计入绩效统计")
        lines.append("")
        lines.append(f"报告生成：{datetime.now(BJ_TZ).strftime('%Y-%m-%d %H:%M:%S')}（UTC+8）")
        return "\n".join(lines)

    lines = []
    lines.append("📊 FVG 自动交易数据·最近24H")
    lines.append("━━━━━━━━━━━━")

    # 一、账户概览
    lines.append("一、账户概览")
    if isinstance(acct, dict) and acct and not acct.get("__error__"):
        init_approx = acct["totalEq"] - net - upl
        ret = (net + upl) / init_approx if init_approx else 0.0
        pf = metrics.get("profit_factor") if metrics.get("sample") else None
        pf_s = f"{pf:.2f}" if pf else "N/A"
        dd_s = f"{metrics['max_drawdown'] * 100:.2f}%" if metrics.get("sample") else "-"
        pct = ret * 100
        pct_s = f"{pct:+.3f}" if abs(pct) >= 0.0005 else "+0.000"
        lines.append(f"总盈亏 {net + upl:+.2f}（含浮盈 {upl:+.2f}） | 净盈亏(已实现) {net:+.2f} | "
                     f"收益率 {pct_s}% | 盈亏比 {pf_s} | 最大回撤 {dd_s}")
    else:
        lines.append(f"账户接口不可用：{acct if acct else 'balance 无返回'}")
    lines.append("")

    # 二、交易概览
    lines.append("二、交易概览")
    lines.append(f"总开仓 {n_open} | 总平仓 {len(closed_stat)} | 总持仓 {n_pos} | 拒单 0")
    if pos_by_contract := {t.get("contract"): 0 for t in trades}:
        for t in trades:
            pos_by_contract[t.get("contract")] += 1
    lines.append("品种：" + " / ".join(f"{k.replace('_USDT', '')}" for k in pos_by_contract)
                 + "  |  多 " + str(dir_stat.get("buy", 0)) + " / 空 " + str(dir_stat.get("sell", 0)))
    lines.append("")

    # 三、分周期×品种绩效（模板口径）
    lines.append("三、分周期绩效（周期×品种）")
    iv_order = {"5m": "5M", "15m": "15M", "1h": "1H", "4h": "4H", "?": "老仓"}
    ivs = sorted({iv for (iv, _c) in period_perf},
                 key=lambda x: {"5m": 0, "15m": 1, "1h": 2, "4h": 3}.get(x, 9))
    if ivs:
        lines.append("| 周期 | 品种 | 订单数 | 止盈/止损/强平 | 盈亏 | 胜率 |")
        lines.append("|------|------|--------|----------------|------|------|")
        for iv in ivs:
            for contract in ("BTC_USDT", "XAU_USDT"):
                ct = contract.replace("_USDT", "")
                p = period_perf.get((iv, contract))
                if not p:
                    lines.append(f"| {iv_order.get(iv, iv.upper())} | {ct} | — | —/—/— | — | — |")
                    continue
                n = p["tp"] + p["sl"] + p["liq"]
                if n == 0:
                    lines.append(f"| {iv_order.get(iv, iv.upper())} | {ct} | — | —/—/— | — | — |")
                    continue
                wr = p["tp"] / n * 100
                lines.append(f"| {iv_order.get(iv, iv.upper())} | {ct} | {n} | {p['tp']}/{p['sl']}/{p['liq']} | {p['pnl']:+.2f}U | {wr:.2f}% |")
    else:
        lines.append("暂无平仓统计")
    lines.append("")

    # 多周期共振（组合×品种，模板口径）
    lines.append("⚡ 多周期共振" + ("（详细）" if n_reso else ""))
    reso_pos_stat = {}
    reso_pos_triple_n, reso_pos_triple_pnl = 0, 0.0
    for r in reso_list:
        if r.get("triple"):
            reso_pos_triple_n += 1
            reso_pos_triple_pnl += r["pnl"] or 0.0
        st = reso_pos_stat.setdefault((r["combo"], r["contract"]), {"n": 0, "pnl": 0.0})
        st["n"] += 1
        st["pnl"] += r["pnl"] or 0.0
    combos = sorted(set([k[0] for k in reso_closed_stats]) | set([k[0] for k in reso_pos_stat]),
                    key=lambda combo: ({"5M+15M": 0, "15M+1H": 1, "5M+1H": 2}.get(combo, 9), combo))
    if combos:
        lines.append("| 共振组合 | 品种 | 订单数 | 止盈/止损/强平 | 盈亏 | 胜率 |")
        lines.append("|----------|------|--------|----------------|------|------|")
        for combo in combos:
            for contract in ("BTC_USDT", "XAU_USDT"):
                ct = contract.replace("_USDT", "")
                cst = reso_closed_stats.get((combo, contract), {"n": 0, "tp": 0, "sl": 0, "liq": 0,
                                                                "realized": 0.0, "fee": 0.0})
                pos_st = reso_pos_stat.get((combo, contract), {"n": 0, "pnl": 0.0})
                if cst["n"] == 0 and pos_st["n"] == 0:
                    continue
                if cst["n"] == 0:
                    lines.append(f"| {combo} | {ct} | 持仓中 {pos_st['n']} | 浮盈 {pos_st['pnl']:+.2f} | — | — |")
                else:
                    wr = cst["tp"] / cst["n"] * 100
                    pos_txt = f" (+持仓{pos_st['n']})" if pos_st["n"] else ""
                    lines.append(f"| {combo} | {ct} | {cst['n']}{pos_txt} | {cst['tp']}/{cst['sl']}/{cst['liq']} | {cst['realized']:+.2f}U | {wr:.2f}% |")
    else:
        lines.append("今日无共振组合")
    if n_reso:
        for r in reso_list:
            pnl_s = f"{r['pnl']:+.2f}" if r["pnl"] is not None else "-"
            tag = "浮亏" if (r["pnl"] or 0) < 0 else "浮盈"
            dir_cn = "多" if r["dir"] in ("long", "buy") else "空"
            lines.append(f"• {r['contract'].replace('_USDT', '')} {r['combo']} {dir_cn} | {r['status']} | "
                         f"开{r['open']} 现{r['cur']} | {tag} {pnl_s}")
    lines.append("")

    # 四、盈亏拆解（账户口径：已实现含手动平仓；平台成交时已划扣手续费，盈亏不含手续费属正常）
    lines.append("四、盈亏拆解")
    lines.append(f"已实现：{realized:+.2f} | 未实现：{upl:+.2f}")
    lines.append(f"手续费：{fees:+.2f} | 资金费：{funding_text}")
    lines.append(f"净盈亏：{net:+.2f} USDT")
    if manual_n:
        lines.append(f"备注：手动平仓 {manual_n} 笔（盈亏 {manual_pnl:+.2f}U）仅备注，不计入绩效统计")
    lines.append("")

    # 五、绩效指标
    lines.append("五、绩效指标")
    if metrics.get("sample"):
        m = metrics
        pf = f"{m['profit_factor']:.2f}" if m["profit_factor"] else "N/A"
        dd = f"{m['max_drawdown'] * 100:.2f}%"
        hold_s = f"{avg_hold_h:.1f}h" if avg_hold_h is not None else "-"
        lines.append(f"胜率：{m['win_rate'] * 100:.0f}% ({m['wins']}/{m['n']}) | 盈亏比：{pf}")
        lines.append(f"最大回撤：{dd} | 平均持仓：{hold_s}")
    else:
        hold_s = f"{avg_hold_h:.1f}h" if avg_hold_h is not None else "-"
        lines.append(f"胜率：0% (0/0) | 盈亏比：N/A")
        lines.append(f"最大回撤：- | 平均持仓：{hold_s}")
    lines.append("")

    # 系统运行
    run_s = f"{run_hours:.0f}h" if run_hours is not None else "-"
    ping = _safe(lambda: okx_exec.ping(), None)
    ping_s = "正常" if isinstance(ping, dict) else f"异常({ping})"
    errs = []
    if not isinstance(acct, dict) or acct.get("__error__"):
        errs.append("balance异常")
    if not isinstance(pos, list):
        errs.append("positions异常")
    if not isinstance(algo, list):
        errs.append("algos异常")
    err_s = "无异常" if not errs else "异常:" + "/".join(errs)
    lines.append("系统运行")
    lines.append(f"运行 {run_s} | API延迟 {ping_s} | {err_s} | 无拒单")
    lines.append("")

    # 总结
    issues = []
    if n_open and not closed_traded:
        issues.append(f"开仓 {n_open} 单尚无平仓（OCO 未触发或 demo fills 未返回），盈亏未实现")
    if n_pos:
        issues.append(f"持有 {n_pos} 仓，注意风控")
    if n_reso:
        issues.append(f"多周期共振 {n_reso} 笔，信号增强")
    if warn:
        issues.append("；".join(warn))
    if not issues:
        issues.append("运行正常")
    lines.append("总结")
    lines.append("；".join(issues))

    if not compact:
        # ===== 完整版附录：平仓明细 / 持仓挂单 / FVG信号 / 系统 =====
        lines.append("")
        lines.append("━━━━━━ 附录 ━━━━━━")
        lines.append("【平仓明细】")
        if closed_traded:
            shown = closed_traded[-8:]
            if len(closed_traded) > len(shown):
                lines.append(f"（共 {len(closed_traded)} 笔，仅展示最近 {len(shown)} 笔）")
            for c in shown:
                arrow = "▲" if c["side"] == "buy" else "▼"
                tag = {"tp": "止盈", "sl": "止损", "liq": "强平", "manual": "手动"}.get(c.get("reason"), "")
                tag_s = f" [{tag}]" if tag else ""
                lines.append(f"- {arrow} {c['ord_id'][-6:]} {_fmt_ts(c['ts'])} 价 {c['px'] or '-'} 净 {c['net']:+.2f}{tag_s}")
        else:
            lines.append("- 当日无平仓成交记录")
        lines.append("【持仓/挂单】")
        if pos_rows_raw:
            for p in pos_rows_raw:
                inst = p.get("instId", "?")
                side = p.get("posSide", "?")
                lines.append(f"- {inst} {side} 开 {_fmt_px(p.get('avgPx'))} 现 {_fmt_px(p.get('markPx'))} "
                             f"浮盈 {float(p.get('upl') or 0.0):+.2f} {p.get('lev') or p.get('lever') or '-'}x")
        else:
            lines.append("- 无持仓")
        if isinstance(algo, list):
            lines.append(f"- OCO 挂单 {len(algo)} 单")
        else:
            lines.append(f"- 挂单接口不可用：{algo}")
        lines.append("【FVG 信号】")
        lines.append(f"- 当日信号数：{len(fvg_keys)} 个（.fvg_pushed 口径）")
        fvg_stat = {}
        for k in fvg_keys:
            parts = k.split("|")
            c = parts[0].replace("_USDT", "") if parts else "?"
            iv = parts[1] if len(parts) > 1 else "?"
            fvg_stat[(c, iv)] = fvg_stat.get((c, iv), 0) + 1
        if fvg_stat:
            lines.append("- 分布：" + " / ".join(f"{c} {iv} {n}个" for (c, iv), n in sorted(fvg_stat.items())))
        lines.append("【系统】")
        if isinstance(acct, dict) and acct and not acct.get("__error__"):
            lines.append(f"- 权益 {acct['totalEq']:.2f} | 可用 {acct['availEq']:.2f} USDT")
        lines.append(f"- 报告生成：{datetime.now(BJ_TZ).strftime('%Y-%m-%d %H:%M:%S')}（UTC+8）")
        if isinstance(gh_runs, list):
            ok = sum(1 for s, _ in gh_runs if s == "success")
            lines.append(f"- Actions 近{len(gh_runs)}次：成功 {ok} / 失败 {len(gh_runs) - ok}")
        else:
            lines.append("- Actions 状态：非 Actions 环境，未查询")

    text = "\n".join(lines)
    if dry:
        text += "\n\n[DRY-RUN 未推送]"
    return text


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
    rolling = args.date is None  # 未指定日期：按推送时刻往前推 24h 滚动窗口
    snapshot = collect_snapshot()
    full = build_report(day_str, base_dir=args.base_dir, compact=False, rolling=rolling,
                        snapshot=snapshot)
    push = build_report(day_str, base_dir=args.base_dir, compact=True, rolling=rolling,
                        snapshot=snapshot)

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

