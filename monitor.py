# -*- coding: utf-8 -*-
"""资源股滞后套利 · 每日自动监控

数据源(全部免费):
  总开关   中证商品期货指数 100001.CCI     ak.futures_index_ccidx
  ①⑤     主力连续合约日线(含持仓量)      ak.futures_main_sina
  ③       生意社期现表(近月/主力价差)     ak.futures_spot_price_daily
  ④       交易所库存(东方财富)           ak.futures_inventory_em
  ②⑥     A股前复权日线(东方财富)        ak.stock_zh_a_hist

无期货的现货品种(稀土/黄磷/MDI): 生意社网页有反爬,暂用 config 中
manual_c30 手工维护 30 日涨幅;为空则该品种标记"缺数据",不产生信号。

用法:  python monitor.py                       # 抓数、算闸门、写 data/<最新收盘日>.json
       python monitor.py 2026-08-18 2026-08-19  # 回填指定日期(按当日收盘口径取数)
"""
import json
import sys
import time
import datetime as dt
from pathlib import Path

import pandas as pd
import akshare as ak

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
R = CFG["rules"]
ASOF = dt.date.today()  # 当前抓取所"截至"的日期, 由 run() 设置


def log(*a):
    print("[monitor]", *a, flush=True)


def retry(fn, n=4, wait=6):
    for i in range(n):
        try:
            return fn()
        except Exception as e:
            if i == n - 1:
                raise
            log("  retry", i + 1, repr(e)[:100])
            time.sleep(wait)


def ret_over_days(df, date_col, close_col, days):
    """最新收盘相对 N 个自然日前(取其前最近交易日)收盘的涨幅 %"""
    d = df.dropna(subset=[close_col]).copy()
    d[date_col] = pd.to_datetime(d[date_col])
    d = d.sort_values(date_col)
    last = d.iloc[-1]
    base_day = last[date_col] - pd.Timedelta(days=days)
    base = d[d[date_col] <= base_day]
    if base.empty:
        return None, last
    return round((last[close_col] / base.iloc[-1][close_col] - 1) * 100, 2), last


def fetch_master():
    df = retry(lambda: ak.futures_index_ccidx(symbol=CFG["master"]["index_name"]))
    df = df[["日期", "收盘点位"]].dropna()
    df["日期"] = pd.to_datetime(df["日期"])
    df = df.sort_values("日期")
    df = df[df["日期"] <= pd.Timestamp(ASOF)]
    ma = df["收盘点位"].rolling(CFG["master"]["ma_window"]).mean()
    last = df.iloc[-1]
    ma_v = ma.iloc[-1]
    return {
        "date": last["日期"].strftime("%Y-%m-%d"),
        "index": round(float(last["收盘点位"]), 1),
        "ma200": round(float(ma_v), 1),
        "dev_pct": round((float(last["收盘点位"]) / float(ma_v) - 1) * 100, 2),
        "on": bool(last["收盘点位"] > ma_v),
    }


def fetch_spot_table():
    """生意社期现表,取 ASOF 当天或其前最近一个交易日"""
    for back in range(6):
        day = (ASOF - dt.timedelta(days=back)).strftime("%Y%m%d")
        try:
            df = ak.futures_spot_price_daily(start_day=day, end_day=day)
            if df is not None and len(df):
                return df
        except Exception:
            continue
    return None


def gate(no, title, rule, value, ok, detail=None):
    """ok: True/False/'na'(免检)/'nd'(缺数据); detail: 展开明细负载"""
    st = ok if ok in ("na", "nd") else ("pass" if ok else "fail")
    g = {"no": no, "title": title, "rule": rule, "value": value, "st": st}
    if detail is not None:
        g["detail"] = detail
    return g


def series_detail(df, date_col, close_col, days):
    """30日窗口的价格序列明细(供前端画走势小图)"""
    d = df.dropna(subset=[close_col]).copy()
    d[date_col] = pd.to_datetime(d[date_col])
    d = d.sort_values(date_col)
    last = d.iloc[-1]
    basedf = d[d[date_col] <= last[date_col] - pd.Timedelta(days=days)]
    base = basedf.iloc[-1] if len(basedf) else d.iloc[0]
    win = d[d[date_col] >= base[date_col]]
    return {
        "type": "series",
        "base": [base[date_col].strftime("%m-%d"), round(float(base[close_col]), 2)],
        "last": [last[date_col].strftime("%m-%d"), round(float(last[close_col]), 2)],
        "series": [[r[date_col].strftime("%m-%d"), round(float(r[close_col]), 2)]
                   for _, r in win.iterrows()],
    }


def variety_gates(v, spot_row):
    gates, meta = [], {}

    # ① 商品启动
    if v.get("spot"):
        c30 = v.get("manual_c30")
        if c30 is None:
            gates.append(gate("①", "商品启动", f"30日涨幅 > {R['c30_threshold']:.0f}%", "缺数据", "nd"))
        else:
            gates.append(gate("①", "商品启动", f"30日涨幅 > {R['c30_threshold']:.0f}%",
                              f"{c30:+.1f}%", c30 > R["c30_threshold"]))
        meta["c30"] = c30
        gates.append(gate("③", "现货紧张", "现货品种免检", "—", "na"))
        gates.append(gate("④", "库存去化", "现货品种免检", "—", "na"))
        gates.append(gate("⑤", "量仓齐升", "现货品种免检", "—", "na"))
        return gates, meta

    fut = retry(lambda: ak.futures_main_sina(
        symbol=v["sina_main"],
        start_date=(ASOF - dt.timedelta(days=120)).strftime("%Y%m%d"),
        end_date=ASOF.strftime("%Y%m%d")))
    c30, _ = ret_over_days(fut, "日期", "收盘价", R["lookback_calendar_days"])
    meta["c30"] = c30
    gates.append(gate("①", "商品启动", f"30日涨幅 > {R['c30_threshold']:.0f}%",
                      f"{c30:+.1f}%" if c30 is not None else "缺数据",
                      "nd" if c30 is None else c30 > R["c30_threshold"],
                      series_detail(fut, "日期", "收盘价", R["lookback_calendar_days"])))

    # ③ 期限结构: 近月 - 远月(主力), 正值 = Back
    if spot_row is not None:
        row = spot_row[spot_row["symbol"] == v["futures"]]
        if len(row):
            r0 = row.iloc[0]
            if str(r0["near_month"]) != str(r0["dominant_month"]):
                back = float(r0["near_contract_price"]) - float(r0["dominant_contract_price"])
            else:
                back = float(r0["spot_price"]) - float(r0["dominant_contract_price"])
            meta["back"] = round(back, 1)
            basis_detail = {
                "type": "basis", "date": str(r0["date"]),
                "spot": round(float(r0["spot_price"]), 1),
                "near": str(r0["near_contract"]), "near_price": round(float(r0["near_contract_price"]), 1),
                "dom": str(r0["dominant_contract"]), "dom_price": round(float(r0["dominant_contract_price"]), 1),
            }
            gates.append(gate("③", "现货紧张", "近月 > 远月 (Back)", f"{back:+.0f}", back > 0, basis_detail))
        else:
            gates.append(gate("③", "现货紧张", "近月 > 远月 (Back)", "缺数据", "nd"))
    else:
        gates.append(gate("③", "现货紧张", "近月 > 远月 (Back)", "缺数据", "nd"))

    # ④ 库存: 周度(每周最后一个交易日)连续 N 周下降
    try:
        inv = retry(lambda: ak.futures_inventory_em(symbol=v["inventory_em"]))
        inv["日期"] = pd.to_datetime(inv["日期"])
        inv = inv[inv["日期"] <= pd.Timestamp(ASOF)]
        wk = inv.set_index("日期")["库存"].resample("W-FRI").last().dropna()
        chg = wk.diff().dropna()
        down = 0
        for x in reversed(chg.tolist()):
            if x < 0:
                down += 1
            else:
                break
        meta["inv_down_weeks"] = down
        wk_detail = {"type": "weekly",
                     "rows": [[i.strftime("%m-%d"), int(v), (int(c) if pd.notna(c) else None)]
                              for (i, v), c in list(zip(wk.items(), wk.diff()))[-8:]]}
        gates.append(gate("④", "库存去化", f"连续 {R['inv_down_weeks']} 周下降",
                          f"{down} 周", down >= R["inv_down_weeks"], wk_detail))
    except Exception as e:
        log("  库存失败", v["name"], repr(e)[:80])
        gates.append(gate("④", "库存去化", f"连续 {R['inv_down_weeks']} 周下降", "缺数据", "nd"))

    # ⑤ 量仓齐升: 最近 N 个交易日中"收涨且增仓"的天数
    f = fut.sort_values("日期").tail(R["oi_window_days"] + 1).reset_index(drop=True)
    days = 0
    for i in range(1, len(f)):
        if f.loc[i, "收盘价"] > f.loc[i - 1, "收盘价"] and f.loc[i, "持仓量"] > f.loc[i - 1, "持仓量"]:
            days += 1
    meta["oi_up_days"] = days
    daily_detail = {"type": "daily", "rows": [
        [f.loc[i, "日期"].strftime("%m-%d") if hasattr(f.loc[i, "日期"], "strftime") else str(f.loc[i, "日期"])[5:10],
         round(float(f.loc[i, "收盘价"]), 1), round(float(f.loc[i, "收盘价"] - f.loc[i - 1, "收盘价"]), 1),
         int(f.loc[i, "持仓量"]), int(f.loc[i, "持仓量"] - f.loc[i - 1, "持仓量"])]
        for i in range(1, len(f))]}
    gates.append(gate("⑤", "量仓齐升", f"近{R['oi_window_days']}日增仓上涨 ≥ {R['oi_up_days']} 天",
                      f"{days} 天", days >= R["oi_up_days"], daily_detail))
    return gates, meta


_em_dead = 0  # 东财连续失败计数, 达到 2 后本轮直接走腾讯


def fetch_stock_hist(code):
    global _em_dead
    end = ASOF.strftime("%Y%m%d")
    start = (ASOF - dt.timedelta(days=70)).strftime("%Y%m%d")
    if _em_dead < 2:
        try:
            df = retry(lambda: ak.stock_zh_a_hist(symbol=code, period="daily",
                                                  start_date=start, end_date=end, adjust="qfq"), n=2)
            _em_dead = 0
            return df
        except Exception:
            _em_dead += 1
            log("  东财失效计数", _em_dead, "→ 降级腾讯")
    # 东财限频时降级到腾讯日线
    sym = ("sh" if code.startswith(("6", "9")) else "sz") + code
    df = retry(lambda: ak.stock_zh_a_hist_tx(symbol=sym, start_date=start, end_date=end, adjust="qfq"))
    df = df.rename(columns={"date": "日期", "close": "收盘"})
    df["涨跌幅"] = (df["收盘"].pct_change() * 100).round(2)
    return df


def stock_gates(code):
    df = fetch_stock_hist(code)
    s30, last = ret_over_days(df, "日期", "收盘", R["lookback_calendar_days"])
    d = df.copy()
    d["日期"] = pd.to_datetime(d["日期"])
    win = d[d["日期"] >= d["日期"].max() - pd.Timedelta(days=R["lookback_calendar_days"])]
    max_drop = float(win["涨跌幅"].min())
    worst = win.dropna(subset=["涨跌幅"]).nsmallest(3, "涨跌幅")
    worst_detail = {"type": "worst",
                    "rows": [[r["日期"].strftime("%m-%d"), round(float(r["涨跌幅"]), 2),
                              round(float(r["收盘"]), 2)] for _, r in worst.iterrows()]}
    gates = [
        gate("②", "股价滞后", f"30日涨幅 < {R['s30_threshold']:.0f}%",
             f"{s30:+.1f}%" if s30 is not None else "缺数据",
             "nd" if s30 is None else s30 < R["s30_threshold"],
             series_detail(df, "日期", "收盘", R["lookback_calendar_days"])),
        gate("⑥", "个股健康", f"30日无单日跌超 {-R['max_single_drop']:.0f}%",
             f"{max_drop:.1f}%", max_drop > R["max_single_drop"], worst_detail),
    ]
    info = {"price": round(float(last["收盘"]), 2),
            "chg_pct": round(float(last["涨跌幅"]), 2),
            "s30": s30, "max_drop": round(max_drop, 1),
            "date": pd.to_datetime(last["日期"]).strftime("%Y-%m-%d")}
    return gates, info


def all_ok(gates):
    return all(g["st"] in ("pass", "na") for g in gates)


def notify(signals, date):
    n = CFG.get("notify", {})
    if not signals:
        return
    text = f"资源股滞后套利 {date} 触发 {len(signals)} 个信号: " + \
        "、".join(f"{s['stock']}({s['variety']})" for s in signals)
    try:
        import requests
        if n.get("bark_url"):
            requests.get(n["bark_url"].rstrip("/") + "/" + text, timeout=10)
        if n.get("serverchan_key"):
            requests.post(f"https://sctapi.ftqq.com/{n['serverchan_key']}.send",
                          data={"title": "滞后套利信号", "desp": text}, timeout=10)
    except Exception as e:
        log("通知失败", repr(e)[:100])


def run(asof):
    global ASOF, _em_dead
    ASOF = asof
    _em_dead = 0
    DATA.mkdir(exist_ok=True)
    log(f"截至 {asof} · 总开关: 商品指数 vs MA200 ...")
    master = fetch_master()
    log(f"  {master['index']} / MA200 {master['ma200']} → {'开启' if master['on'] else '关闭'}")

    log("生意社期现表 ...")
    spot_row = fetch_spot_table()

    varieties, signals = [], []
    for v in CFG["pool"]:
        log("品种:", v["name"])
        try:
            vg, meta = variety_gates(v, spot_row)
        except Exception as e:
            log("  品种级失败", repr(e)[:120])
            vg, meta = [gate("①", "商品启动", "抓取失败", "错误", "nd")], {}
        v_pass = all_ok(vg)
        v_nd = any(g["st"] == "nd" for g in vg)

        stocks = []
        for s in v["stocks"]:
            try:
                sg, info = stock_gates(s["code"])
            except Exception as e:
                log("  个股失败", s["name"], repr(e)[:80])
                sg, info = [gate("②", "股价滞后", "抓取失败", "错误", "nd")], {}
            sig = bool(master["on"] and v_pass and not v_nd and all_ok(sg)
                       and not any(g["st"] == "nd" for g in sg))
            stocks.append({**s, **info, "gates": sg, "signal": sig})
            if sig:
                signals.append({"variety": v["name"], "stock": s["name"], "code": s["code"],
                                "grade": v["grade"], "cap": v["cap"]})
            time.sleep(1.5)

        varieties.append({
            "name": v["name"], "grade": v["grade"], "cap": v["cap"],
            "futures": (v.get("futures") or "") + (" 主力" if v.get("futures") else ""),
            "spot": bool(v.get("spot")), "spot_desc": v.get("spot_desc", ""),
            "meta": meta, "gates": vg, "stocks": stocks,
        })

    date = master["date"]
    out = {"date": date, "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
           "master": master, "varieties": varieties, "signals": signals}
    (DATA / f"{date}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    idx_file = DATA / "index.json"
    idx = json.loads(idx_file.read_text(encoding="utf-8")) if idx_file.exists() else []
    if date not in idx:
        idx.append(date)
    idx = sorted(idx, reverse=True)[:60]
    idx_file.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")

    log(f"完成: {len(signals)} 个信号 → data/{date}.json")
    for s in signals:
        log("  ✓", s["variety"], s["stock"], s["code"], s["cap"])
    notify(signals, date)


def main():
    dates = sys.argv[1:]
    if not dates:
        return run(dt.date.today())
    for d in dates:
        run(dt.datetime.strptime(d, "%Y-%m-%d").date())


if __name__ == "__main__":
    sys.exit(main())
