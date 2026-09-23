# -*- coding: utf-8 -*-
"""
查尔斯交易信号决策链 · OKX 执行模块（Demo 模拟盘）

职责：
  - 行情读取（公开接口）：OKX 永续合约 K线 / 资金费率，作为 gate-futures / weex 之后的第三备源
  - Demo 模拟盘下单/平仓：通过 x-simulated-trading: 1 头访问 OKX 模拟环境（不涉及真实资金）
  - 账户查询：余额 / 持仓（供 okx_demo 模式校验下单结果）

合约代码映射（OKX 永续 SWAP）：
  BTC_USDT  -> BTC-USDT-SWAP
  XAU_USDT  -> XAU-USDT-SWAP   （OKX 黄金永续，XAUUSD-SWAP 返回 51001 不可用）

凭证来源（按优先级）：
  OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE 环境变量
  本地开发可写入 .env_okx.json（不入库），云端一律走 GitHub Secrets
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import time

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://www.okx.com"
TIMEOUT = 15
# demo 环境标识头：所有交易/账户请求必须携带，避免误触实盘
SIMULATED_HEADER = {"x-simulated-trading": "1"}

# 与 datafeed 的 inst 字段映射
_INST_MAP = {
    "BTC_USDT": "BTC-USDT-SWAP",
    "XAU_USDT": "XAU-USDT-SWAP",
}
# OKX bar 参数（注意 OKX 用大写 H/D）
_INTERVAL_MAP = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
                 "1h": "1H", "4h": "4H", "1d": "1D"}


def _okx_inst(inst: str) -> str:
    """把 datafeed 的 inst（如 BTC_USDT）转为 OKX SWAP 代码"""
    return _INST_MAP.get(inst, inst)


def _load_creds() -> dict:
    """读取 OKX API 凭证：环境变量优先，其次本地 .env_okx.json（不入库）"""
    env = {k: os.environ.get(k, "") for k in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE")}
    if all(env.values()):
        return env
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env_okx.json")
    if os.path.exists(local):
        try:
            with open(local, "r", encoding="utf-8") as f:
                d = json.load(f)
            env = {k: d.get(k, env[k]) for k in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE")}
        except Exception as e:  # noqa: BLE001
            log.warning("读取 .env_okx.json 失败: %s", e)
    return env


def _sign(timestamp: str, method: str, path: str, body: str, secret: str) -> str:
    """OKX v5 签名：HMAC-SHA256(timestamp + method + requestPath + body) 后 base64"""
    msg = timestamp + method + path + body
    mac = hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


def _iso_ts() -> str:
    """OKX 时间戳格式：UTC ISO8601 带毫秒"""
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


class OkxError(RuntimeError):
    pass


def _public_get(path: str, params: dict) -> dict:
    """公开接口（行情/合约信息，无需签名）"""
    r = requests.get(BASE_URL + path, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "0":
        raise OkxError(f"OKX公开接口失败 {path}: {data.get('code')} {data.get('msg')}")
    return data


def _private_request(method: str, path: str, body: dict = None, params: dict = None) -> dict:
    """私有接口（账户/交易），强制带 x-simulated-trading: 1，绝不打真实盘"""
    creds = _load_creds()
    if not all(creds.values()):
        raise OkxError("OKX 凭证缺失：请设置 OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE")
    body_str = json.dumps(body) if body else ""
    qs = ("?" + requests.compat.urlencode(params)) if params else ""
    full_path = path + qs
    ts = _iso_ts()
    sign = _sign(ts, method, full_path, body_str, creds["OKX_API_SECRET"])
    headers = {
        "OK-ACCESS-KEY": creds["OKX_API_KEY"],
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": creds["OKX_API_PASSPHRASE"],
        "Content-Type": "application/json",
        **SIMULATED_HEADER,
    }
    r = requests.request(method, BASE_URL + full_path, headers=headers,
                         data=body_str, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "0":
        raise OkxError(f"OKX私有接口失败 {method} {path}: {data.get('code')} {data.get('msg')}")
    return data


# ---------------- 行情（公开，供 datafeed 第三备源） ----------------

def fetch_klines(inst: str, interval: str = "1h", limit: int = 120) -> list:
    """OKX 永续 K线，返回统一格式 [{ts, open, high, low, close, volume}] 时间升序。
    OKX 返回最新在前，须排序。bar 参数用 OKX 大写（1H/4H/1D）。"""
    bar = _INTERVAL_MAP.get(interval)
    if not bar:
        raise OkxError(f"OKX不支持周期 {interval}")
    inst_id = _okx_inst(inst)
    data = _public_get("/api/v5/market/candles",
                       {"instId": inst_id, "bar": bar, "limit": min(limit, 300)})
    out = []
    for row in data.get("data", []):
        # row: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        out.append({
            "ts": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5] or 0),
        })
    return sorted(out, key=lambda x: x["ts"])


def fetch_funding_rate(inst: str) -> dict:
    """OKX 当前资金费率（公开），返回 {"rate", "cycle", "next_ts", "ts"}"""
    inst_id = _okx_inst(inst)
    data = _public_get("/api/v5/public/funding-rate", {"instId": inst_id})
    row = data.get("data", [{}])[0]
    return {
        "rate": float(row.get("fundingRate", 0.0) or 0.0),
        "cycle": int(row.get("fundingRate", 0) and 8 * 3600),  # OKX 永续每 8h 结算一次
        "next_ts": int(row.get("fundingTime", 0) or 0),
        "ts": int(row.get("ts", 0) or 0),
    }


def get_instruments(inst: str) -> dict:
    """查询 SWAP 合约规格（ctVal 面值 / lotSz 下单单位），供张数换算"""
    inst_id = _okx_inst(inst)
    data = _public_get("/api/v5/public/instruments",
                       {"instType": "SWAP", "instId": inst_id})
    if not data.get("data"):
        raise OkxError(f"OKX合约 {inst_id} 不存在")
    row = data["data"][0]
    return {
        "instId": row["instId"],
        "ctVal": float(row.get("ctVal", 1.0) or 1.0),
        "lotSz": float(row.get("lotSz", 1.0) or 1.0),
        "minSz": float(row.get("minSz", 0.0) or 0.0),
        "tickSz": float(row.get("tickSz", 0.0) or 0.0),
    }


# ---------------- 账户与交易（Demo，仅模拟盘） ----------------

def get_balance() -> float:
    """获取模拟账户 USDT 可用余额"""
    data = _private_request("GET", "/api/v5/account/balance")
    details = data.get("data", [{}])[0].get("details", [])
    for d in details:
        if d.get("ccy") == "USDT":
            return float(d.get("availEq", d.get("availBal", 0.0)) or 0.0)
    return 0.0


def get_positions(inst: str = None) -> list:
    """查询模拟账户持仓。inst 形如 BTC_USDT；不传则返回全部。
    返回 [{instId, posSide, pos(张数,带方向), avgPx, upl, ...}]"""
    params = {}
    if inst:
        params["instId"] = _okx_inst(inst)
    data = _private_request("GET", "/api/v5/account/positions", params=params)
    return data.get("data", [])


def _contract_qty(size_usdt: float, price: float, inst: str, leverage: int = 100) -> float:
    """按名义金额(USDT)换算 OKX 合约张数：张数 = 名义 / (价格 × 面值ctVal)，向上取整到 lotSz"""
    spec = get_instruments(inst)
    ct_val = spec["ctVal"]
    if price <= 0 or ct_val <= 0:
        raise OkxError(f"换算张数失败: size={size_usdt} price={price} ctVal={ct_val}")
    notional_per_ct = price * ct_val
    # size_usdt 为成本(保证金)，名义金额 = 成本 × 杠杆，张数 = 名义 / 单张价值
    qty = size_usdt * leverage / notional_per_ct
    lot = spec["lotSz"]
    if lot and lot > 0:
        import math
        qty = math.ceil(qty / lot) * lot
    return round(qty, 8)


def place_market_order(inst: str, side: str, size_usdt: float, price: float,
                       leverage: int = 100, reduce_only: bool = False) -> dict:
    """市价开/平仓（仅 Demo 模拟盘）。
    inst: BTC_USDT / XAU_USDT；side: buy(开多/平空) / sell(开空/平多)。
    返回 OKX 下单结果 ordId。
    注意：市价单成交价未知，此处传入 price 仅用于估算张数。
    双向持仓(posSide)推断：开仓 buy→long / sell→short；reduce_only 平仓 sell→long / buy→short。"""
    inst_id = _okx_inst(inst)
    qty = _contract_qty(size_usdt, price, inst, leverage)
    if qty <= 0:
        raise OkxError(f"下单张数为0: size={size_usdt} price={price}")
    # 双向持仓(posSide)推断：开多buy→long / 开空sell→short / 平多sell→long / 平空buy→short
    pos_side = "short" if (side == "buy") == reduce_only else "long"
    body = {
        "instId": inst_id,
        "tdMode": "isolated",
        "side": side,
        "posSide": pos_side,
        "ordType": "market",
        "sz": str(qty),
    }
    if reduce_only:
        body["reduceOnly"] = "true"
    data = _private_request("POST", "/api/v5/trade/order", body=body)
    row = data.get("data", [{}])[0]
    ord_id = row.get("ordId", "")
    result = {"ordId": ord_id, "qty": qty, "instId": inst_id, "posSide": pos_side}
    # 市价单成交回报回填：查询订单详情取实际成交均价 avgPx 与成交张数 accFillSz，
    # 供上层将推送/账本的 entry、margin 与 OKX 实际撮合保持一致。
    try:
        detail = _private_request("GET", "/api/v5/trade/order",
                                  params={"instId": inst_id, "ordId": ord_id})
        drow = detail.get("data", [{}])[0] if detail.get("data") else {}
        avg_px = float(drow.get("avgPx") or 0.0)
        fill_sz = float(drow.get("accFillSz") or 0.0)
        result["avgPx"] = avg_px
        result["fillQty"] = fill_sz if fill_sz > 0 else qty
        log.info("OKX订单成交回报: ordId=%s avgPx=%s fillQty=%s", ord_id, avg_px, result["fillQty"])
    except Exception as e:  # noqa: BLE001
        log.warning("OKX订单详情查询失败(保持估算值): %s", e)
        result["avgPx"] = 0.0
        result["fillQty"] = 0.0
    return result


def set_leverage(inst: str, leverage: int = 100) -> dict:
    """设置模拟账户逐仓杠杆（下单前调用，确保 100x）。
    双向持仓模式下逐仓需分别指定 posSide，此处对 long/short 都设置。
    若目标杠杆超品种上限（如 XAU 上限低于 100x 报 59102），自动探测最大杠杆并降级，
    返回 {"lever_used": 实际生效杠杆, "resp": ...}，供上层同步账本，保证账本与 OKX 一致。"""
    inst_id = _okx_inst(inst)
    lever_used = leverage
    last = None
    for pos_side in ("long", "short"):
        body = {"instId": inst_id, "lever": str(lever_used), "mgnMode": "isolated", "posSide": pos_side}
        try:
            last = _private_request("POST", "/api/v5/account/set-leverage", body=body)
            if last and str(last.get("code", "")) != "0":
                log.warning("OKX设置杠杆(%s/%s)失败 code=%s，自动探测品种上限并降级",
                            inst_id, pos_side, last.get("code"))
                max_lev = get_max_leverage(inst)
                if max_lev < lever_used:
                    lever_used = max_lev
                    body["lever"] = str(lever_used)
                    last = _private_request("POST", "/api/v5/account/set-leverage", body=body)
                    log.info("OKX杠杆已降级: %s %sx(目标%sx)", inst_id, lever_used, leverage)
        except Exception as e:  # noqa: BLE001
            log.warning("OKX设置杠杆(%s/%s)失败: %s", inst_id, pos_side, e)
    return {"lever_used": lever_used, "resp": last or {}}


def get_max_leverage(inst: str, td_mode: str = "isolated") -> int:
    """查询 OKX 该合约支持的最大杠杆（public position-tiers，所有档位取 max）。
    用于 XAU 等品种杠杆上限低于 100x 时自动降级，保持账本与 OKX 一致。"""
    inst_id = _okx_inst(inst)
    data = _public_get("/api/v5/public/position-tiers",
                       {"instType": "SWAP", "tdMode": td_mode, "instId": inst_id})
    rows = data.get("data", [])
    if not rows:
        return 100
    max_lev = max(int(float(r.get("maxLever") or 0)) for r in rows)
    return max_lev if max_lev > 0 else 100


def place_algo_sltp(inst: str, direction: str, qty: float, sl_px: float, tp_px: float) -> dict:
    """为持仓挂 OKX 止盈止损组合算法单（order-algo，ordType=oco，止盈+止损双边）。
    direction: long/short 持仓方向；qty: 张数（需等于全部持仓，即"全部仓位"）；sl_px/tp_px: 触发价。
    由交易所端实时触发，App 可见挂单；止盈止损任一侧触发后另一侧自动失效。
    注意：OKX v5 中 conditional 为单边（止盈或止损二选一），双边必须用 oco，
    否则同时传 tp+sl 时 OKX 只保留 sl、tp 丢失（App 上只显示止损）。返回 algoId。"""
    inst_id = _okx_inst(inst)
    side = "sell" if direction == "long" else "buy"
    pos_side = "long" if direction == "long" else "short"
    body = {
        "instId": inst_id,
        "tdMode": "isolated",
        "side": side,
        "posSide": pos_side,
        "ordType": "oco",
        "sz": str(qty),
        "tpTriggerPx": str(tp_px),
        "tpOrdPx": "-1",
        "slTriggerPx": str(sl_px),
        "slOrdPx": "-1",
    }
    data = _private_request("POST", "/api/v5/trade/order-algo", body=body)
    row = data.get("data", [{}])[0]
    return {"algoId": row.get("algoId", ""), "instId": inst_id, "posSide": pos_side}


def cancel_algo(inst: str, algo_id: str, retries: int = 2) -> dict:
    """撤销止盈止损算法单（order-algo），平仓前调用避免残留挂单。
    失败自动重试 retries 次，并在重试结束后强制校验 algo-pending：
    若确认仍挂在待触发列表，返回 {"force_cleanup": True}，由上层决定继续重试还是告警。"""
    inst_id = _okx_inst(inst)
    last = None
    for attempt in range(retries + 1):
        try:
            body = {"algoId": [{"instId": inst_id, "algoId": algo_id}]}
            last = _private_request("POST", "/api/v5/trade/cancel-algos", body=body)
            # OKX cancel-algos 正常返回 code=0；返回其它 code 视为失败
            if last and str(last.get("code", "")) == "0":
                log.info("OKX撤销算法单成功: inst=%s algoId=%s", inst_id, algo_id)
                return last
            code = last.get("code", "?") if last else "no-response"
            log.warning("OKX撤销算法单失败 code=%s (attempt %d/%d)", code, attempt + 1, retries + 1)
        except Exception as e:  # noqa: BLE001
            last = {"code": "exception", "msg": str(e)}
            log.warning("OKX撤销算法单异常: %s (attempt %d/%d)", e, attempt + 1, retries + 1)
        if attempt < retries:
            import time
            time.sleep(1.5 * (attempt + 1))
    # 重试结束后强制校验：查询 pending 列表确认该 algoId 是否残留
    try:
        pend = get_algo_orders(inst=inst, algo_id=algo_id)
        if any(str(a.get("algoId", "")) == str(algo_id) for a in pend):
            log.warning("OKX强制校验: algoId=%s 仍残留于待触发列表", algo_id)
            return {"force_cleanup": True, "algoId": algo_id, "instId": inst_id, "resp": last}
    except Exception as e:  # noqa: BLE001
        log.warning("OKX撤销后校验失败: %s", e)
    return last or {}


def get_algo_orders(inst: str = None, algo_id: str = None, ord_type: str = "oco") -> list:
    """查询未触发/有效的算法单列表。OKX orders-algo-pending 接口 ordType 为必填参数，
    且没有 state 参数（state 仅存在于 orders-algo-history），传 state 会 400。
    inst 形如 BTC_USDT；algoId 可选精确查询。"""
    params = {"ordType": ord_type}
    if inst:
        params["instId"] = _okx_inst(inst)
    if algo_id:
        params["algoId"] = algo_id
    data = _private_request("GET", "/api/v5/trade/orders-algo-pending", params=params)
    return data.get("data", [])


def ping() -> dict:
    """连通性检查：公开时间接口"""
    data = _public_get("/api/v5/public/time", {})
    return data


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("OKX 时间接口:", ping())
    for i in ("BTC_USDT", "XAU_USDT"):
        try:
            spec = get_instruments(i)
            print(i, "->", spec)
            k = fetch_klines(i, "1h", 3)
            print("  1H K线 x", len(k), "最新", k[-1]["close"] if k else "N/A")
            fr = fetch_funding_rate(i)
            print("  资金费率:", fr["rate"])
        except Exception as e:  # noqa: BLE001
            print(i, "失败:", e)
    try:
        print("模拟盘余额 USDT:", get_balance())
    except Exception as e:  # noqa: BLE001
        print("余额查询失败(本地无凭证/连不通属正常):", e)
