#!/usr/bin/env python3
"""
spinoff_monitor.py — 每月追蹤「大公司分拆 / 賣仔上市」新股

功能:
  discover  : 抓取 stockanalysis.com 的分拆表 + IPO 表, 找出未在 watchlist 的新項目
  check     : 對 watchlist 每隻股, 取現價 / 52週高位, 判斷有冇跌穿上市價
  report    : check + 輸出 Markdown 報告同 CSV (預設 check 已包含)

用法:
  python3 spinoff_monitor.py init          # 建立 watchlist.json (內置 2026 年名單)
  python3 spinoff_monitor.py discover      # 找新上市 (分拆自動加入, IPO 列為候選)
  python3 spinoff_monitor.py check         # 每月檢視, 出報告
  python3 spinoff_monitor.py add TICKER --name "X" --parent "Y" --type ipo \
          --date 2026-08-01 --ref-price 20.00

檔案 (同目錄):
  watchlist.json  追蹤名單
  candidates.json discover 找到但未確認母公司的 IPO
  history.csv     每次 check 的快照 (可用嚟畫走勢)
  report_YYYY-MM-DD.md  當月報告
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, date, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
WATCHLIST = os.path.join(BASE, "watchlist.json")
CANDIDATES = os.path.join(BASE, "candidates.json")
HISTORY = os.path.join(BASE, "history.csv")

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"}

# 跌穿呢個 % 就當警號 (相對 52 週高位)
DRAWDOWN_ALERT = 20.0

# ---------------------------------------------------------------- 內置種子名單
SEED = [
    # 分拆派股 (spinoff): ref_price = 首日收市價, 由 script 自動補
    dict(ticker="VSNT", name="Versant Media Group",      parent="Comcast (CMCSA)",   type="spinoff", list_date="2026-01-05", ref_price=None),
    dict(ticker="RNA",  name="Atrium Therapeutics",      parent="Avidity Bio",       type="spinoff", list_date="2026-02-26", ref_price=None),
    dict(ticker="MMED", name="MiniMed Group",            parent="Medtronic (MDT)",   type="ipo",     list_date="2026-03-06", ref_price=20.00),
    dict(ticker="PAYP", name="PayPay Corporation",       parent="SoftBank (SBG)",    type="ipo",     list_date="2026-03-12", ref_price=16.00),
    dict(ticker="VGNT", name="Versigent PLC",            parent="Aptiv (APTV)",      type="spinoff", list_date="2026-04-01", ref_price=None),
    dict(ticker="TRAX", name="First Tracks Bio",         parent="AnaptysBio (ANAB)", type="spinoff", list_date="2026-04-20", ref_price=None),
    dict(ticker="OCTV", name="Octave Intelligence",      parent="Hexagon AB",        type="spinoff", list_date="2026-05-28", ref_price=None),
    dict(ticker="FDXF", name="FedEx Freight",            parent="FedEx (FDX)",       type="spinoff", list_date="2026-06-01", ref_price=None),
    dict(ticker="QNT",  name="Quantinuum",               parent="Honeywell (HON)",   type="ipo",     list_date="2026-06-04", ref_price=60.00),
    dict(ticker="HONA", name="Honeywell Aerospace",      parent="Honeywell (HON)",   type="spinoff", list_date="2026-06-29", ref_price=None),
    dict(ticker="MBGL", name="Mobility Global",          parent="S&P Global (SPGI)", type="spinoff", list_date="2026-07-01", ref_price=None),
    dict(ticker="MFP",  name="Midera Food Processing",   parent="Middleby (MIDD)",   type="spinoff", list_date="2026-07-06", ref_price=None),
    dict(ticker="ADIG", name="ADI Global Distribution",  parent="Resideo (REZI)",    type="spinoff", list_date="2026-08-04", ref_price=None),
]


# ---------------------------------------------------------------- 小工具
def load(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def fetch(url, tries=3, sleep=2):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:          # noqa: BLE001
            last = e
            time.sleep(sleep * (i + 1))
    raise last


# ---------------------------------------------------------------- Yahoo 報價
def _quote_yahoo(ticker, host):
    url = (f"https://{host}/v8/finance/chart/{ticker}"
           f"?range=2y&interval=1d")
    data = json.loads(fetch(url, tries=2, sleep=3))
    res = (data.get("chart") or {}).get("result")
    if not res:
        raise ValueError("Yahoo 冇資料")
    meta = res[0]["meta"]
    closes = [c for c in (res[0]["indicators"]["quote"][0].get("close") or []) if c]
    if not meta.get("regularMarketPrice"):
        raise ValueError("Yahoo 冇報價")
    return {
        "price": meta.get("regularMarketPrice"),
        "high52": meta.get("fiftyTwoWeekHigh"),
        "low52": meta.get("fiftyTwoWeekLow"),
        "currency": meta.get("currency", "USD"),
        "first_close": closes[0] if closes else None,
        "source": "yahoo",
    }


def _quote_stooq(ticker):
    """後備source: Stooq 日線 CSV, 唔會封 datacenter IP"""
    url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"
    txt = fetch(url, tries=2, sleep=2)
    lines = [l for l in txt.strip().splitlines() if l and l[0].isdigit()]
    if not lines:
        raise ValueError("Stooq 冇資料")
    rows = []
    for l in lines:
        p = l.split(",")
        if len(p) >= 5:
            try:
                rows.append((p[0], float(p[2]), float(p[4])))   # date, high, close
            except ValueError:
                pass
    if not rows:
        raise ValueError("Stooq 資料格式唔啱")
    cutoff = (date.today().toordinal() - 365)
    last_year = [r for r in rows
                 if datetime.strptime(r[0], "%Y-%m-%d").date().toordinal() >= cutoff]
    return {
        "price": rows[-1][2],
        "high52": max(r[1] for r in (last_year or rows)),
        "low52": None,
        "currency": "USD",
        "first_close": rows[0][2],
        "source": "stooq",
    }


def quote(ticker):
    """依次試 Yahoo query1 / query2 / Stooq, 全部失敗就拋出最後一個錯"""
    errs = []
    for fn, label in ((lambda: _quote_yahoo(ticker, "query1.finance.yahoo.com"), "y1"),
                      (lambda: _quote_yahoo(ticker, "query2.finance.yahoo.com"), "y2"),
                      (lambda: _quote_stooq(ticker), "stooq")):
        try:
            return fn()
        except Exception as e:      # noqa: BLE001
            errs.append(f"{label}:{type(e).__name__}"
                        f"{getattr(e, 'code', '') and ' ' + str(e.code)}")
    raise ValueError(" / ".join(errs))


def profile(ticker):
    """回傳 (sector, industry, longname) — 用 Yahoo search endpoint, 唔使 crumb"""
    url = (f"https://query2.finance.yahoo.com/v1/finance/search"
           f"?q={ticker}&quotesCount=5&newsCount=0")
    data = json.loads(fetch(url))
    for q in data.get("quotes", []):
        if q.get("symbol", "").upper() == ticker.upper():
            return (q.get("sectorDisp") or q.get("sector") or "?",
                    q.get("industryDisp") or q.get("industry") or "?",
                    q.get("longname") or q.get("shortname"))
    return "?", "?", None


# ---------------------------------------------------------------- discover
SPINOFF_URL = "https://stockanalysis.com/actions/spinoffs/{year}/"
IPO_URL = "https://stockanalysis.com/ipos/{year}/"

ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")


def _cells(row_html):
    return [TAG_RE.sub("", c).replace("&amp;", "&").strip()
            for c in CELL_RE.findall(row_html)]


HEADER_WORDS = {"date", "ipo date", "symbol", "parent", "new stock", "company",
                "company name", "parent company", "new company", "ipo price",
                "current", "return", "ratio", "ticker"}


def _looks_like_header(cells):
    lowered = [c.strip().lower() for c in cells[:6]]
    return sum(1 for c in lowered if c in HEADER_WORDS) >= 2


def parse_table(html):
    out = []
    for row in ROW_RE.findall(html):
        c = _cells(row)
        if len(c) < 4 or not c[0]:
            continue
        if _looks_like_header(c):          # 跳過表頭, 唔好當佢係一隻股
            continue
        if not re.match(r"^[A-Za-z]{1,6}$", c[1].strip()) and \
           not re.match(r"^[A-Z][a-z]{2}\s+\d", c[0].strip()):
            continue                        # 第一格唔似日期, 第二格唔似代號 -> 唔要
        out.append(c)
    return out


def discover(year=None):
    year = year or date.today().year
    wl = load(WATCHLIST, [])
    known = {x["ticker"].upper() for x in wl}
    cands = load(CANDIDATES, [])
    known_c = {x["ticker"].upper() for x in cands}
    added, new_cands = [], []

    # 1) 分拆表 — 有母公司資料, 直接入 watchlist
    try:
        rows = parse_table(fetch(SPINOFF_URL.format(year=year)))
        for c in rows:
            # Date | Parent | New Stock | Parent Company | New Company
            if len(c) < 5:
                continue
            d, parent_sym, new_sym, parent_name, new_name = c[:5]
            if new_sym.upper() in known:
                continue
            added.append(dict(ticker=new_sym.upper(), name=new_name,
                              parent=f"{parent_name} ({parent_sym})",
                              type="spinoff", list_date=_norm_date(d),
                              ref_price=None))
            known.add(new_sym.upper())
    except Exception as e:      # noqa: BLE001
        print(f"[warn] 攞唔到分拆表: {e}", file=sys.stderr)

    # 2) IPO 表 — 冇母公司欄, 只能列做候選畀你人手確認係咪 carve-out
    try:
        rows = parse_table(fetch(IPO_URL.format(year=year)))
        for c in rows:
            # IPO Date | Symbol | Company Name | IPO Price | Current | Return
            if len(c) < 4:
                continue
            d, sym, name, ipo_price = c[0], c[1].upper(), c[2], c[3]
            if sym in known or sym in known_c:
                continue
            if _is_spac(name):          # SPAC 唔理
                continue
            new_cands.append(dict(ticker=sym, name=name, parent="?",
                                  type="ipo", list_date=_norm_date(d),
                                  ref_price=_money(ipo_price)))
            known_c.add(sym)
    except Exception as e:      # noqa: BLE001
        print(f"[warn] 攞唔到 IPO 表: {e}", file=sys.stderr)

    if added:
        wl.extend(added)
        wl.sort(key=lambda x: x.get("list_date") or "")
        save(WATCHLIST, wl)
    if new_cands:
        cands.extend(new_cands)
        save(CANDIDATES, cands)

    print(f"分拆新加入 watchlist: {len(added)}")
    for a in added:
        print(f"  + {a['ticker']:6s} {a['name']}  ← {a['parent']}")
    print(f"IPO 候選 (要你確認係咪大公司分拆出嚟): {len(new_cands)}")
    for a in new_cands:
        print(f"  ? {a['ticker']:6s} {a['name']}  ({a['list_date']}, IPO ${a['ref_price']})")
    if new_cands:
        print("\n確認咗就用:  python3 spinoff_monitor.py add TICKER "
              "--parent \"母公司 (SYM)\" --type ipo --date YYYY-MM-DD --ref-price 20.00")


SPAC_WORDS = ("acquisition corp", "acquisition co", "acquisition corporation",
              "capital corp", "merger corp", "spac", "holdings acquisition",
              "acquisition i", "acquisition ii", "acquisition iii")


def _is_spac(name):
    n = name.lower()
    return any(w in n for w in SPAC_WORDS)


def _money(s):
    m = re.search(r"[\d.]+", (s or "").replace(",", ""))
    return float(m.group()) if m else None


def _norm_date(s):
    for fmt in ("%b %d, %Y", "%Y-%m-%d", "%b %d %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return s.strip()


# ---------------------------------------------------------------- check
def check(write_report=True):
    wl = load(WATCHLIST, [])
    if not wl:
        print("watchlist 係空嘅, 先行 `init`", file=sys.stderr)
        return 1

    today = date.today().isoformat()
    rows, dirty = [], False

    for item in wl:
        t = item["ticker"]
        try:
            q = quote(t)
        except Exception as e:      # noqa: BLE001
            print(f"[warn] {t}: {e}", file=sys.stderr)
            err = f"NO DATA ({e})"[:60]
            rows.append(dict(ticker=t, name=item["name"], parent=item["parent"],
                             type=item["type"], list_date=item.get("list_date", ""),
                             sector=item.get("sector", "?"),
                             industry=item.get("industry", "?"),
                             ref=item.get("ref_price"), ref_note="",
                             price=None, high52=None, vs_ref=None,
                             vs_high=None, flags=err))
            continue

        # 板塊只查一次, 之後 cache 落 watchlist
        if not item.get("sector"):
            try:
                s, ind, _ = profile(t)
                item["sector"], item["industry"] = s, ind
                dirty = True
            except Exception:       # noqa: BLE001
                pass            # 查唔到就留空, 下次再試, 唔好覆蓋已有資料

        # 分拆股冇招股價, 用首日收市價做基準, 只記一次
        ref = item.get("ref_price")
        if ref is None and q["first_close"]:
            ref = round(q["first_close"], 2)
            item["ref_price"] = ref
            item["ref_note"] = "首日收市價"
            dirty = True

        price, high52 = q["price"], q["high52"]
        vs_ref = (price / ref - 1) * 100 if (ref and price) else None
        vs_high = (price / high52 - 1) * 100 if (high52 and price) else None

        flags = []
        if vs_ref is not None and vs_ref < 0:
            flags.append("跌穿上市價")
        if vs_high is not None and vs_high <= -DRAWDOWN_ALERT:
            flags.append(f"距52週高 {vs_high:.0f}%")
        if high52 and price and price >= high52 * 0.999:
            flags.append("創52週新高")

        rows.append(dict(ticker=t, name=item["name"], parent=item["parent"],
                         type=item["type"], list_date=item.get("list_date", ""),
                         sector=item.get("sector", "?"),
                         industry=item.get("industry", "?"),
                         ref=ref, ref_note=item.get("ref_note", "招股價"),
                         price=price, high52=high52,
                         source=q.get("source", ""),
                         vs_ref=vs_ref, vs_high=vs_high, flags=", ".join(flags) or "—"))

    if dirty:
        save(WATCHLIST, wl)

    _append_history(today, rows)
    text = _render(today, rows)
    print(text)
    if write_report:
        p = os.path.join(BASE, f"report_{today}.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"\n[報告已寫入 {p}]")
    return 0


def _append_history(day, rows):
    new = not os.path.exists(HISTORY)
    with open(HISTORY, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "ticker", "name", "parent", "type", "list_date",
                        "sector", "industry", "ref_price", "ref_basis",
                        "price", "high52", "pct_vs_ref", "pct_vs_high52", "flags"])
        for r in rows:
            w.writerow([day, r["ticker"], r["name"], r["parent"], r["type"],
                        r.get("list_date", ""), r.get("sector", ""),
                        r.get("industry", ""), _f(r["ref"]), r.get("ref_note", ""),
                        _f(r["price"]), _f(r.get("high52")),
                        _f(r["vs_ref"]), _f(r["vs_high"]), r["flags"]])


def _f(v, nd=2):
    return "" if v is None else f"{v:.{nd}f}"


def _render(day, rows):
    L = [f"# 分拆 / 分拆上市新股週度檢視 — {day}", ""]
    alert = [r for r in rows if r["flags"] not in ("—", "NO DATA")]
    below = [r for r in rows if (r["vs_ref"] or 0) < 0 and r["price"]]
    L.append(f"追蹤 {len(rows)} 隻｜跌穿上市價 {len(below)} 隻｜有警號 {len(alert)} 隻")
    L.append("")

    hdr = ("| 代號 | 公司 | 母公司 | 板塊 | 行業 | 類型 | 上市日 | 上市價 | "
           "現價 | vs 上市價 | 52週高 | vs 52週高 | 狀態 |")
    sep = "|" + "---|" * 13

    def block(title, items):
        if not items:
            return
        L.append(f"## {title}")
        L.append("")
        L.append(hdr)
        L.append(sep)
        for r in items:
            L.append("| {t} | {n} | {p} | {s} | {i} | {ty} | {d} | {ref} | {pr} | "
                     "{vr} | {hi} | {vh} | {fl} |".format(
                         t=r["ticker"], n=r["name"], p=r["parent"],
                         s=r.get("sector", "?"), i=r.get("industry", "?"),
                         ty="IPO" if r["type"] == "ipo" else "分拆",
                         d=r.get("list_date", ""), ref=_f(r["ref"]),
                         pr=_f(r["price"]), vr=_pct(r["vs_ref"]),
                         hi=_f(r.get("high52")), vh=_pct(r["vs_high"]),
                         fl=r["flags"]))
        L.append("")

    key = lambda x: (x["vs_ref"] is None, x["vs_ref"] or 0)  # noqa: E731
    block("⚠️ 跌穿上市價", sorted(below, key=key))
    block("✅ 高於上市價", sorted([r for r in rows if r not in below], key=key, reverse=True))

    # 板塊分佈
    L.append("## 板塊分佈")
    L.append("")
    L.append("| 板塊 | 隻數 | 平均 vs 上市價 |")
    L.append("|---|---|---|")
    buckets = {}
    for r in rows:
        buckets.setdefault(r.get("sector", "?"), []).append(r)
    for s, items in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        vals = [i["vs_ref"] for i in items if i["vs_ref"] is not None]
        avg = sum(vals) / len(vals) if vals else None
        L.append(f"| {s} | {len(items)} | {_pct(avg)} |")
    L.append("")
    L.append("*上市價：IPO 股用招股價；分拆股冇招股價，用首日收市價代替。*")
    return "\n".join(L)


def _pct(v):
    return "" if v is None else f"{v:+.1f}%"


# ---------------------------------------------------------------- 其他指令
def init(force=False):
    if os.path.exists(WATCHLIST) and not force:
        print(f"{WATCHLIST} 已經存在，加 --force 覆蓋")
        return 1
    save(WATCHLIST, SEED)
    print(f"已建立 {WATCHLIST}（{len(SEED)} 隻）")
    return 0


def add(args):
    wl = load(WATCHLIST, [])
    if any(x["ticker"].upper() == args.ticker.upper() for x in wl):
        print("已經喺 watchlist 度")
        return 1
    wl.append(dict(ticker=args.ticker.upper(), name=args.name or args.ticker,
                   parent=args.parent or "?", type=args.type,
                   list_date=args.date, ref_price=args.ref_price))
    wl.sort(key=lambda x: x.get("list_date") or "")
    save(WATCHLIST, wl)
    # 加入咗就順手喺 candidates 度剔走
    cands = [c for c in load(CANDIDATES, []) if c["ticker"].upper() != args.ticker.upper()]
    save(CANDIDATES, cands)
    print(f"已加入 {args.ticker.upper()}")
    return 0


def export_sheet(path=None):
    """出一個畀 Google Sheets 用嘅 CSV: 只有靜態資料, 價格交畀 GOOGLEFINANCE"""
    wl = load(WATCHLIST, [])
    path = path or os.path.join(BASE, "watchlist.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "name", "parent", "type", "list_date",
                    "sector", "industry", "ref_price", "ref_basis"])
        for x in sorted(wl, key=lambda i: i.get("list_date") or ""):
            w.writerow([x["ticker"], x["name"], x["parent"],
                        "IPO" if x["type"] == "ipo" else "Spinoff",
                        x.get("list_date", ""), x.get("sector", ""),
                        x.get("industry", ""), x.get("ref_price", ""),
                        x.get("ref_note", "招股價")])
    print(f"已寫入 {path}（{len(wl)} 隻）")
    return 0


def main():
    ap = argparse.ArgumentParser(description="分拆/分拆上市新股月度監察")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init"); p.add_argument("--force", action="store_true")
    p = sub.add_parser("discover"); p.add_argument("--year", type=int)
    sub.add_parser("check")
    sub.add_parser("export")
    p = sub.add_parser("add")
    p.add_argument("ticker")
    p.add_argument("--name")
    p.add_argument("--parent")
    p.add_argument("--type", choices=["ipo", "spinoff"], default="ipo")
    p.add_argument("--date")
    p.add_argument("--ref-price", type=float, dest="ref_price")
    p = sub.add_parser("run")   # discover + check, 畀 cron 用

    a = ap.parse_args()
    if a.cmd == "init":
        return init(a.force)
    if a.cmd == "discover":
        return discover(a.year) or 0
    if a.cmd == "check":
        return check()
    if a.cmd == "export":
        return export_sheet()
    if a.cmd == "add":
        return add(a)
    if a.cmd == "run":
        if not os.path.exists(WATCHLIST):
            init()
        discover()
        export_sheet()
        try:
            return check()
        except Exception as e:      # noqa: BLE001
            print(f"[info] 報價攞唔到 ({e}), 但 watchlist.csv 已更新", file=sys.stderr)
            return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
