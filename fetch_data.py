#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ERP-ALL 留資成效追蹤 — 每日抓數腳本
---------------------------------------------------
從 GA4 Data API + Google Search Console API 抓取 erp-all 頁面的成效指標，
寫入 docs/data/history.json，供靜態儀表板 (docs/index.html) 前端渲染。

不含任何機敏值：Property ID / GSC 網址 / 服務帳號皆由「環境變數」提供，
本機讀 .env（見專案根 .env），雲端讀 GitHub Secret。

需要的環境變數：
  GA4_PROPERTY_ID              GA4 資源 ID（純數字）
  GSC_PROPERTY_URL             Search Console 資源網址（可選；未授權則自動略過）
  GOOGLE_APPLICATION_CREDENTIALS  服務帳號 JSON 檔路徑
"""
import os
import sys
import json
import datetime as dt
from pathlib import Path
from urllib.parse import urlparse

# Windows 主控台預設 cp950，無法印 ✓／中文，改用 utf-8（不影響 Linux runner）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---- 路徑 ----
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
OUT = DATA_DIR / "history.json"
CONFIG = json.loads((DATA_DIR / "config.json").read_text(encoding="utf-8"))

PAGE_FILTER = CONFIG.get("page_filter", "erp-all")
HISTORY_DAYS = int(CONFIG.get("history_window_days", 90))

# 行業關鍵字：用來從 content_click 中篩出「行業方案分流」點擊
INDUSTRY_KEYWORDS = [
    "行業", "產業", "製造", "金屬", "醫材", "餐飲", "紡織", "機械", "電子",
    "食品", "化工", "建材", "汽車", "零售", "批發", "工具機", "光電", "半導體",
    "五金", "塑膠", "橡膠", "營建", "科技業", "服務業", "方案",
]

PROPERTY_ID = os.environ.get("GA4_PROPERTY_ID", "").strip()
GSC_URL = os.environ.get("GSC_PROPERTY_URL", "").strip()
SA_PATH = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()

ERPALL_URL = "https://www.digiwin.com.tw/ERP/erp-all.html"
_ASSET_RE = None


def fetch_erpall_links():
    """爬 erp-all 取得所有可導航站內連結，當作『真下一頁』白名單。失敗回 None。"""
    import re
    import urllib.request
    try:
        req = urllib.request.Request(ERPALL_URL, headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    except Exception as e:
        print(f"  ⚠ 爬 erp-all 連結失敗，略過白名單過濾：{type(e).__name__}", file=sys.stderr)
        return None
    hrefs = re.findall(r'href\s*=\s*["\']([^"\']+)["\']', html, re.I)
    asset = re.compile(r'\.(css|js|png|jpe?g|gif|svg|ico|woff2?|ttf)(\?|$)', re.I)
    wl = set()
    for h in hrefs:
        h = h.strip().split("#")[0].split("?")[0]
        if not h or h.startswith(("javascript:", "mailto:", "tel:")):
            continue
        if h.startswith("http"):
            if "digiwin.com" not in h:
                continue
            h = re.sub(r"^https?://[^/]+", "", h)
        if not h.startswith("/") or asset.search(h):
            continue
        wl.add(h)
        wl.add(h.rstrip("/"))
    return wl or None


def taipei_now_iso():
    tz = dt.timezone(dt.timedelta(hours=8))
    return dt.datetime.now(tz).strftime("%Y-%m-%d %H:%M")


def daterange_strs(days):
    end = dt.date.today() - dt.timedelta(days=1)          # 到昨天（今日資料未定案）
    start = end - dt.timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


# ============================ GA4 ============================
def fetch_ga4():
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import (
        RunReportRequest, DateRange, Dimension, Metric,
        FilterExpression, FilterExpressionList, Filter, OrderBy,
    )

    client = BetaAnalyticsDataClient()
    prop = f"properties/{PROPERTY_ID}"
    start, end = daterange_strs(HISTORY_DAYS)

    HOST = CONFIG.get("host", "").strip()

    def host_eq():
        return FilterExpression(filter=Filter(
            field_name="hostName",
            string_filter=Filter.StringFilter(
                match_type=Filter.StringFilter.MatchType.EXACT, value=HOST)))

    def with_host(fe):
        # 鎖定網域，避免 GA4 把多網域相同路徑併在一起（如各站的 "/"）
        if not HOST:
            return fe
        return FilterExpression(and_group=FilterExpressionList(expressions=[host_eq(), fe]))

    def page_contains():
        return with_host(FilterExpression(filter=Filter(
            field_name="pagePath",
            string_filter=Filter.StringFilter(
                match_type=Filter.StringFilter.MatchType.CONTAINS, value=PAGE_FILTER),
        )))

    def event_eq(name):
        return FilterExpression(filter=Filter(
            field_name="eventName",
            string_filter=Filter.StringFilter(
                match_type=Filter.StringFilter.MatchType.EXACT, value=name),
        ))

    def run(dims, mets, dim_filter=None, order=None, limit=None):
        req = RunReportRequest(
            property=prop,
            date_ranges=[DateRange(start_date=start, end_date=end)],
            dimensions=[Dimension(name=d) for d in dims],
            metrics=[Metric(name=m) for m in mets],
            dimension_filter=dim_filter,
            order_bys=order or [],
            limit=limit or 100000,
        )
        return client.run_report(req)

    out = {}

    # 1) 每日：訪客 / 工作階段 / 互動率 / 跳出率 / 平均觀看頁數
    r = run(
        ["date"],
        ["totalUsers", "sessions", "engagementRate", "bounceRate", "screenPageViewsPerSession"],
        dim_filter=page_contains(),
        order=[OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name="date"))],
    )
    daily = {}
    for row in r.rows:
        d = row.dimension_values[0].value
        d = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
        v = [x.value for x in row.metric_values]
        daily[d] = {
            "date": d,
            "users": int(float(v[0])),
            "sessions": int(float(v[1])),
            "engagement_rate": round(float(v[2]) * 100, 1),
            "bounce_rate": round(float(v[3]) * 100, 1),
            "views_per_session": round(float(v[4]), 2),
            "leads": 0,
        }

    # 2) 每日：generate_lead 事件數（當頁留資）
    r = run(
        ["date"], ["eventCount"],
        dim_filter=FilterExpression(and_group=FilterExpressionList(
            expressions=[event_eq("generate_lead"), page_contains()])),
    )
    for row in r.rows:
        d = row.dimension_values[0].value
        d = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
        leads = int(float(row.metric_values[0].value))
        if d in daily:
            daily[d]["leads"] = leads
        else:
            daily[d] = {"date": d, "users": 0, "sessions": 0, "engagement_rate": 0,
                        "bounce_rate": 0, "views_per_session": 0, "leads": leads}

    for d in daily.values():
        d["lead_rate"] = round(d["leads"] / d["users"] * 100, 2) if d["users"] else 0.0
    out["daily"] = [daily[k] for k in sorted(daily.keys())]

    # ---- 期間對期間比較：近 N 天 vs 前一個 N 天（每一區都要能看出漲跌）----
    W = int(CONFIG.get("compare_window_days", 28))

    def window(days, back):
        end = dt.date.today() - dt.timedelta(days=1 + back)
        start = end - dt.timedelta(days=days - 1)
        return start.isoformat(), end.isoformat()

    cur_s, cur_e = window(W, 0)      # 近 N 天
    prv_s, prv_e = window(W, W)      # 前一個 N 天

    def breakdown(dim, metric, start, end, dim_filter, limit=200):
        req = RunReportRequest(
            property=prop,
            date_ranges=[DateRange(start_date=start, end_date=end)],
            dimensions=[Dimension(name=dim)],
            metrics=[Metric(name=metric)],
            dimension_filter=dim_filter,
            order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name=metric), desc=True)],
            limit=limit,
        )
        d = {}
        for row in client.run_report(req).rows:
            d[row.dimension_values[0].value or "(未定義)"] = int(float(row.metric_values[0].value))
        return d

    def rel_delta(cur, prev):
        return None if not prev else round((cur - prev) / prev * 100, 1)

    click_filter = FilterExpression(and_group=FilterExpressionList(
        expressions=[event_eq("content_click"), page_contains()]))
    lead_filter = FilterExpression(and_group=FilterExpressionList(
        expressions=[event_eq("generate_lead"), page_contains()]))

    # 3) 流量來源：不只看量，更看「各來源留資率」——量漲了若不留資也沒用
    cur_src = breakdown("sessionDefaultChannelGroup", "sessions", cur_s, cur_e, page_contains())
    prv_src = breakdown("sessionDefaultChannelGroup", "sessions", prv_s, prv_e, page_contains())
    cur_src_l = breakdown("sessionDefaultChannelGroup", "eventCount", cur_s, cur_e, lead_filter)
    prv_src_l = breakdown("sessionDefaultChannelGroup", "eventCount", prv_s, prv_e, lead_filter)
    tot = sum(cur_src.values()) or 1
    src = []
    for k, v in sorted(cur_src.items(), key=lambda x: -x[1]):
        lr = cur_src_l.get(k, 0) / v * 100 if v else 0
        pv = prv_src.get(k, 0)
        plr = prv_src_l.get(k, 0) / pv * 100 if pv else 0
        src.append({
            "channel": k, "sessions": v, "pct": round(v / tot * 100, 1),
            "prev_sessions": pv, "delta": rel_delta(v, pv),
            "leads": cur_src_l.get(k, 0), "lead_rate": round(lr, 2),
            "prev_lead_rate": round(plr, 2),
            "lead_rate_delta_pp": round(lr - plr, 2) if pv else None,
        })
    out["source_mix"] = src

    # 4) 裝置：重點不是流量占比，而是「各裝置留資率」及其變化
    #    對應 report 結論：行動裝置能見度瓶頸 → 行動留資率是主要觀察對象
    cur_dev_u = breakdown("deviceCategory", "totalUsers", cur_s, cur_e, page_contains())
    prv_dev_u = breakdown("deviceCategory", "totalUsers", prv_s, prv_e, page_contains())
    cur_dev_l = breakdown("deviceCategory", "eventCount", cur_s, cur_e, lead_filter)
    prv_dev_l = breakdown("deviceCategory", "eventCount", prv_s, prv_e, lead_filter)
    tot_u = sum(cur_dev_u.values()) or 1
    dev = []
    for k, u in sorted(cur_dev_u.items(), key=lambda x: -x[1]):
        cr = cur_dev_l.get(k, 0) / u * 100 if u else 0
        pu = prv_dev_u.get(k, 0)
        pr = prv_dev_l.get(k, 0) / pu * 100 if pu else 0
        dev.append({
            "device": k, "users": u, "pct": round(u / tot_u * 100, 1),
            "leads": cur_dev_l.get(k, 0),
            "lead_rate": round(cr, 2),
            "prev_lead_rate": round(pr, 2),
            "lead_rate_delta_pp": round(cr - pr, 2) if pu else None,
        })
    out["device"] = dev

    # 5) content_click 點擊排行（含前期對照）
    cur_clk = breakdown("customEvent:click_text", "eventCount", cur_s, cur_e, click_filter, limit=80)
    prv_clk = breakdown("customEvent:click_text", "eventCount", prv_s, prv_e, click_filter, limit=300)

    def norm(t):
        return "(未命名)" if t in ("(not set)", "(not_set)", "") else t

    clicks = []
    for k, v in sorted(cur_clk.items(), key=lambda x: -x[1]):
        clicks.append({"text": norm(k), "clicks": v,
                       "prev_clicks": prv_clk.get(k, 0), "delta": rel_delta(v, prv_clk.get(k, 0))})
    out["content_click"] = clicks[:25]

    # 6) 行業方案分流：從點擊中篩行業關鍵字（含前期對照）
    out["industry_click"] = [
        c for c in clicks if any(k in c["text"] for k in INDUSTRY_KEYWORDS)
    ][:15]

    # 6b) 點擊 × 裝置（手機/桌機各點了什麼；對應 report 行動瓶頸）
    def two_dim(d1, d2, start, end, dim_filter, limit=2000):
        req = RunReportRequest(
            property=prop,
            date_ranges=[DateRange(start_date=start, end_date=end)],
            dimensions=[Dimension(name=d1), Dimension(name=d2)],
            metrics=[Metric(name="eventCount")],
            dimension_filter=dim_filter,
            limit=limit,
        )
        d = {}
        for row in client.run_report(req).rows:
            txt = row.dimension_values[0].value or ""
            dev = row.dimension_values[1].value or ""
            d.setdefault(txt, {})[dev] = int(float(row.metric_values[0].value))
        return d

    cd = two_dim("customEvent:click_text", "deviceCategory", cur_s, cur_e, click_filter)
    dev_clicks = []
    for raw, dv in cd.items():
        desk, mob, tab = dv.get("desktop", 0), dv.get("mobile", 0), dv.get("tablet", 0)
        dev_clicks.append({"text": norm(raw), "desktop": desk, "mobile": mob, "tablet": tab,
                           "total": desk + mob + tab})
    # 熱點分析：每個元素占「全頁點擊」比重，並拆各裝置內部的點擊分布
    grand = sum(x["total"] for x in dev_clicks)
    td = sum(x["desktop"] for x in dev_clicks)
    tm = sum(x["mobile"] for x in dev_clicks)
    for x in dev_clicks:
        x["share"] = round(x["total"] / grand * 100, 1) if grand else 0        # 占全頁點擊
        x["desktop_share"] = round(x["desktop"] / td * 100, 1) if td else 0     # 占桌機點擊
        x["mobile_share"] = round(x["mobile"] / tm * 100, 1) if tm else 0       # 占手機點擊
        x["mobile_lean"] = x["mobile_share"] > x["desktop_share"]              # 手機相對更愛點
    dev_clicks.sort(key=lambda x: -x["total"])
    out["click_device"] = dev_clicks[:15]
    out["click_device_totals"] = {"desktop": td, "mobile": tm,
                                   "tablet": sum(x["tablet"] for x in dev_clicks), "grand": grand}

    # 7) 分流樞紐：前一頁（怎麼來的）／後一頁（往哪去，是否符合設計導流）
    # 標題對照：直接用 GA4 的 pageTitle（涵蓋全站所有頁），titles.json 僅備援
    def path_title_map(start, end):
        req = RunReportRequest(
            property=prop,
            date_ranges=[DateRange(start_date=start, end_date=end)],
            dimensions=[Dimension(name="pagePath"), Dimension(name="pageTitle")],
            metrics=[Metric(name="screenPageViews")],
            dimension_filter=host_eq() if HOST else None,   # 鎖網域，"/" 才會對到 digiwin 首頁而非別站
            order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="screenPageViews"), desc=True)],
            limit=5000,
        )
        m = {}
        for row in client.run_report(req).rows:
            p, t = row.dimension_values[0].value, row.dimension_values[1].value
            if p and t and p not in m:      # 已依瀏覽量排序，首見即最常見標題
                m[p] = t
        return m

    ga4_titles = path_title_map(cur_s, cur_e)

    titles = {}
    tp = DATA_DIR / "titles.json"
    if tp.exists():
        try:
            titles = json.loads(tp.read_text(encoding="utf-8"))
        except Exception:
            titles = {}

    def title_of(path):
        t = ga4_titles.get(path) or ga4_titles.get(path.rstrip("/"))
        if not t:
            rec = titles.get(path) or titles.get(path.rstrip("/")) or {}
            t = rec.get("title", "") if isinstance(rec, dict) else ""
        t = (t or "").replace("｜", "|").split("|")[0].strip()
        return t[:34] if t else path

    def ref_contains(val):
        # 後一頁也鎖同網域：落地頁須在 www.digiwin.com.tw（跨站如就享知自然排除）
        return with_host(FilterExpression(filter=Filter(
            field_name="pageReferrer",
            string_filter=Filter.StringFilter(
                match_type=Filter.StringFilter.MatchType.CONTAINS, value=val))))

    def agg_bucket(data, fn):
        b = {}
        for k, v in data.items():
            b[fn(k)] = b.get(fn(k), 0) + v
        return b

    # 前一頁：erp-all pageview 的來源頁 referrer
    cur_prev = breakdown("pageReferrer", "screenPageViews", cur_s, cur_e, page_contains(), limit=200)
    prv_prev = breakdown("pageReferrer", "screenPageViews", prv_s, prv_e, page_contains(), limit=200)

    def bucket_prev(url):
        u = (url or "").lower()
        if u == "":
            return "直接進入 / 無來源"
        if "google" in u or "syndicatedsearch" in u:
            return "Google 廣告/搜尋"
        if "bing" in u:
            return "Bing 搜尋"
        if "erp-all" in u:
            return "erp-all 本頁(重載/錨點)"
        if "digiwin.com" in u:
            return "站內其他頁導入"
        return "其他外部"

    cb, pb = agg_bucket(cur_prev, bucket_prev), agg_bucket(prv_prev, bucket_prev)
    tot_p = sum(cb.values()) or 1
    out["prev_page"] = [{"label": k, "views": v, "pct": round(v / tot_p * 100, 1),
                         "delta": rel_delta(v, pb.get(k, 0))}
                        for k, v in sorted(cb.items(), key=lambda x: -x[1])]

    # 前一頁明細：依「區塊」分組（外部來源／站內其他頁／erp-all本頁／直接進入），組內列實際頁
    def classify_ref(url):
        if not url:
            return ("直接進入", "直接進入 / 無來源")
        p = urlparse(url)
        host = (p.netloc or "").lower()
        if not host:
            return ("直接進入", "直接 / App 內開啟")
        if "digiwin.com" in host:
            path = p.path or "/"
            if "erp-all" in path.lower():
                return ("erp-all 本頁", "erp-all 本頁(重載/錨點)")
            lab = title_of(path)
            return ("站內其他頁", lab if lab != path else path)
        return ("外部來源", host)

    cpd, ppd, lab_grp = {}, {}, {}
    for url, v in cur_prev.items():
        g, lab = classify_ref(url)
        cpd[lab] = cpd.get(lab, 0) + v
        lab_grp[lab] = g
    for url, v in prv_prev.items():
        _, lab = classify_ref(url)
        ppd[lab] = ppd.get(lab, 0) + v
    # 分組：小計/百分比用完整資料算（分母＝全部前一頁），組內列前 8 項
    tot_pd = sum(cpd.values()) or 1
    pgroups = {}
    for lab, v in cpd.items():
        pg = pgroups.setdefault(lab_grp[lab], {"views": 0, "items": []})
        pg["views"] += v
        pg["items"].append({"label": lab, "views": v, "delta": rel_delta(v, ppd.get(lab, 0))})
    out["prev_grouped"] = [{"group": g, "views": pg["views"], "pct": round(pg["views"] / tot_pd * 100, 1),
                            "count": len(pg["items"]),
                            "items": sorted(pg["items"], key=lambda x: -x["views"])[:8]}
                           for g, pg in sorted(pgroups.items(), key=lambda x: -x[1]["views"])]

    # 後一頁：referrer 含 erp-all 的 pagePath（從 erp-all 點出去的頁）
    cur_next = breakdown("pagePath", "screenPageViews", cur_s, cur_e, ref_contains("erp-all"), limit=300)
    prv_next = breakdown("pagePath", "screenPageViews", prv_s, prv_e, ref_contains("erp-all"), limit=300)

    # 用 erp-all 實際連結白名單篩掉「不是下一頁」的資料：排除本頁(重載/錨點)、
    # 只留 erp-all 有連結的頁 ∪ 動態表單流程（表單/完成頁不在靜態 HTML，明確放行）
    wl = fetch_erpall_links()
    CONV = ("/contact/eform", "/contact/success")

    def is_next(path):
        p = (path or "").split("#")[0].split("?")[0]
        if "erp-all" in p.lower():
            return False                       # 同頁，非下一頁
        if any(c in p for c in CONV):
            return True
        if wl is None:
            return True                        # 爬取失敗 → 不做連結過濾（仍排除本頁）
        return p in wl or p.rstrip("/") in wl

    stayed = sum(v for k, v in cur_next.items() if "erp-all" in (k or "").lower())
    dropped = sum(v for k, v in cur_next.items() if not is_next(k) and "erp-all" not in (k or "").lower())
    cur_next = {k: v for k, v in cur_next.items() if is_next(k)}
    prv_next = {k: v for k, v in prv_next.items() if is_next(k)}

    def bucket_next(path):
        p = (path or "").lower()
        if "/dsc/" in p:
            return "行業方案頁"
        if "/contact/eform" in p:
            return "聯絡表單"
        if "/contact/success" in p:
            return "留資完成頁"
        if "erp-all" in p:
            return "erp-all 本頁(錨點/重載)"
        if p == "/":
            return "首頁"
        if "/software" in p or "/wf" in p:
            return "軟體/WF 頁"
        return "其他頁"

    cbn, pbn = agg_bucket(cur_next, bucket_next), agg_bucket(prv_next, bucket_next)
    tot_n = sum(cbn.values()) or 1
    out["next_page"] = [{"label": k, "views": v, "pct": round(v / tot_n * 100, 1),
                         "delta": rel_delta(v, pbn.get(k, 0))}
                        for k, v in sorted(cbn.items(), key=lambda x: -x[1])]

    # 設計導流達成率：符合「erp-all → 行業方案 / 表單」設計意圖的比例
    out["routing"] = {
        "to_industry_pct": round(cbn.get("行業方案頁", 0) / tot_n * 100, 1),
        "to_form_pct": round((cbn.get("聯絡表單", 0) + cbn.get("留資完成頁", 0)) / tot_n * 100, 1),
        "design_pct": round((cbn.get("行業方案頁", 0) + cbn.get("聯絡表單", 0)
                             + cbn.get("留資完成頁", 0)) / tot_n * 100, 1),
        "total_next_views": tot_n,
        "stayed_on_page": stayed,            # 停留本頁(重載/錨點)，不計入下一頁
        "dropped_nonlink": dropped,          # 非 erp-all 連結被排除的量
        "whitelist_used": wl is not None,
        "whitelist_size": len(wl) if wl else 0,
    }

    # 後一頁：依「去向區塊」分組，小計/百分比用完整資料（分母＝全部後一頁 = tot_n，與首頁漏點卡一致）
    ngroups = {}
    for path, v in cur_next.items():
        ng = ngroups.setdefault(bucket_next(path), {"views": 0, "items": []})
        ng["views"] += v
        ng["items"].append({"label": title_of(path), "path": path, "views": v,
                            "delta": rel_delta(v, prv_next.get(path, 0)), "industry": "/dsc/" in path.lower()})
    out["next_grouped"] = [{"group": g, "views": ng["views"], "pct": round(ng["views"] / tot_n * 100, 1),
                            "count": len(ng["items"]),
                            "items": sorted(ng["items"], key=lambda x: -x["views"])[:8]}
                           for g, ng in sorted(ngroups.items(), key=lambda x: -x[1]["views"])]

    # 8) 廣告表現：付費(cpc)整體 + 各檔期(campaign)的留資（廣告佔進站 8 成，獨立一區）
    def and_(*ex):
        return FilterExpression(and_group=FilterExpressionList(expressions=list(ex)))

    paid = FilterExpression(filter=Filter(
        field_name="sessionMedium",
        string_filter=Filter.StringFilter(match_type=Filter.StringFilter.MatchType.CONTAINS, value="cpc")))

    def scalar(metric, start, end, filt):
        req = RunReportRequest(property=prop, date_ranges=[DateRange(start_date=start, end_date=end)],
                               metrics=[Metric(name=metric)], dimension_filter=filt, limit=1)
        rows = client.run_report(req).rows
        return int(float(rows[0].metric_values[0].value)) if rows else 0

    paid_page = and_(page_contains(), paid)
    paid_lead = and_(page_contains(), paid, event_eq("generate_lead"))

    ps_c, ps_p = scalar("sessions", cur_s, cur_e, paid_page), scalar("sessions", prv_s, prv_e, paid_page)
    pl_c, pl_p = scalar("eventCount", cur_s, cur_e, paid_lead), scalar("eventCount", prv_s, prv_e, paid_lead)
    all_s = scalar("sessions", cur_s, cur_e, page_contains()) or 1
    lr_c = round(pl_c / ps_c * 100, 2) if ps_c else 0
    lr_p = round(pl_p / ps_p * 100, 2) if ps_p else 0

    cur_camp = breakdown("sessionCampaignName", "sessions", cur_s, cur_e, paid_page, limit=50)
    prv_camp = breakdown("sessionCampaignName", "sessions", prv_s, prv_e, paid_page, limit=50)
    cur_camp_l = breakdown("sessionCampaignName", "eventCount", cur_s, cur_e, paid_lead, limit=50)
    prv_camp_l = breakdown("sessionCampaignName", "eventCount", prv_s, prv_e, paid_lead, limit=50)
    camps = []
    for k, v in sorted(cur_camp.items(), key=lambda x: -x[1])[:10]:
        lr = cur_camp_l.get(k, 0) / v * 100 if v else 0
        pv = prv_camp.get(k, 0)
        plr = prv_camp_l.get(k, 0) / pv * 100 if pv else 0
        camps.append({"campaign": k, "sessions": v, "leads": cur_camp_l.get(k, 0),
                      "lead_rate": round(lr, 2), "delta": rel_delta(v, pv),
                      "lead_rate_delta_pp": round(lr - plr, 2) if pv else None})
    out["ads"] = {
        "sessions": ps_c, "leads": pl_c, "lead_rate": lr_c, "share_pct": round(ps_c / all_s * 100, 1),
        "prev": {"sessions": ps_p, "leads": pl_p, "lead_rate": lr_p},
        "delta": {"sessions": rel_delta(ps_c, ps_p), "lead_rate_pp": round(lr_c - lr_p, 2) if ps_p else None},
        "campaigns": camps,
    }

    out["windows"] = {"current": [cur_s, cur_e], "previous": [prv_s, prv_e]}
    return out


# ============================ GSC ============================
def fetch_gsc():
    # GSC_PROPERTY_URL 可能是逗號分隔的多個資源，逐一嘗試，挑能查到 erp-all 資料的那個
    candidates = [u.strip() for u in GSC_URL.split(",") if u.strip()]
    if not candidates:
        return {"available": False, "note": "未設定 GSC_PROPERTY_URL", "daily": [], "queries": [], "totals": {}}
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_file(
            SA_PATH, scopes=["https://www.googleapis.com/auth/webmasters.readonly"])
        svc = build("searchconsole", "v1", credentials=creds, cache_discovery=False)

        # GSC 資料延遲約 2~3 天；比較近 W 天 vs 前一個 W 天
        W = int(CONFIG.get("compare_window_days", 28))
        lag = 3
        today = dt.date.today()

        def win(back):
            e = today - dt.timedelta(days=lag + back)
            s = e - dt.timedelta(days=W - 1)
            return s.isoformat(), e.isoformat()

        cur_s, cur_e = win(0)
        prv_s, prv_e = win(W)
        hist_s = (today - dt.timedelta(days=lag + HISTORY_DAYS - 1)).isoformat()
        page_filter = {"dimension": "page", "operator": "contains", "expression": PAGE_FILTER}

        def raw_query(site, s, e, dimensions, limit):
            body = {"startDate": s, "endDate": e, "dimensions": dimensions,
                    "dimensionFilterGroups": [{"filters": [page_filter]}], "rowLimit": limit}
            return svc.searchanalytics().query(siteUrl=site, body=body).execute()

        # 挑資源：能查通且（最好）有 erp-all 資料的
        chosen, last_err = None, None
        for site in candidates:
            try:
                probe = raw_query(site, cur_s, cur_e, ["date"], 1)
                chosen = site
                if probe.get("rows"):
                    break
            except Exception as e:
                last_err = e
                continue
        if not chosen:
            return {"available": False,
                    "note": f"所有 GSC 資源查詢皆失敗：{type(last_err).__name__}: {last_err}",
                    "daily": [], "queries": [], "totals": {}}

        def q(s, e, dimensions, limit=25):
            return raw_query(chosen, s, e, dimensions, limit)

        def agg(s, e):
            rows = q(s, e, [], 1).get("rows", [])   # 無維度 → 單列總計，排名為 GSC 加權值
            if not rows:
                return {"clicks": 0, "impressions": 0, "ctr": 0, "position": 0}
            r = rows[0]
            return {"clicks": int(r.get("clicks", 0)), "impressions": int(r.get("impressions", 0)),
                    "ctr": round(r.get("ctr", 0) * 100, 2), "position": round(r.get("position", 0), 1)}

        def rd(c, p):
            return None if not p else round((c - p) / p * 100, 1)

        cur_t, prv_t = agg(cur_s, cur_e), agg(prv_s, prv_e)
        totals = {
            "clicks": cur_t["clicks"], "impressions": cur_t["impressions"],
            "ctr": cur_t["ctr"], "position": cur_t["position"], "prev": prv_t,
            "delta": {
                "clicks": rd(cur_t["clicks"], prv_t["clicks"]),
                "impressions": rd(cur_t["impressions"], prv_t["impressions"]),
                "ctr_pp": round(cur_t["ctr"] - prv_t["ctr"], 2) if prv_t["impressions"] else None,
                "position_pp": round(cur_t["position"] - prv_t["position"], 1) if prv_t["position"] else None,
            },
        }

        daily = []
        for row in q(hist_s, cur_e, ["date"], 1000).get("rows", []):
            daily.append({"date": row["keys"][0], "clicks": int(row.get("clicks", 0)),
                          "impressions": int(row.get("impressions", 0)),
                          "ctr": round(row.get("ctr", 0) * 100, 2),
                          "position": round(row.get("position", 0), 1)})

        prev_q = {r["keys"][0]: int(r.get("clicks", 0)) for r in q(prv_s, prv_e, ["query"], 200).get("rows", [])}
        queries = []
        for row in q(cur_s, cur_e, ["query"], 25).get("rows", []):
            clk = int(row.get("clicks", 0))
            queries.append({"query": row["keys"][0], "clicks": clk,
                            "impressions": int(row.get("impressions", 0)),
                            "ctr": round(row.get("ctr", 0) * 100, 2),
                            "position": round(row.get("position", 0), 1),
                            "delta": rd(clk, prev_q.get(row["keys"][0], 0))})   # 點擊 vs 前期

        return {"available": True, "range": [cur_s, cur_e], "prev_range": [prv_s, prv_e],
                "daily": daily, "queries": queries, "totals": totals}
    except Exception as e:
        return {"available": False, "note": f"GSC 未授權或抓取失敗：{type(e).__name__}: {e}",
                "daily": [], "queries": [], "totals": {}}


def main():
    if not PROPERTY_ID:
        print("ERROR: 缺少環境變數 GA4_PROPERTY_ID", file=sys.stderr)
        sys.exit(1)
    if not SA_PATH or not Path(SA_PATH).exists():
        print("ERROR: GOOGLE_APPLICATION_CREDENTIALS 未指向有效的服務帳號檔", file=sys.stderr)
        sys.exit(1)

    result = {"updated_at": taipei_now_iso(), "status": "ok",
              "window_days": int(CONFIG.get("compare_window_days", 28)),
              "history_days": HISTORY_DAYS}

    print("→ 抓 GA4 …")
    result["ga4"] = fetch_ga4()
    print(f"  GA4 每日資料 {len(result['ga4']['daily'])} 天，點擊 {len(result['ga4']['content_click'])} 項")

    print("→ 抓 GSC …")
    result["gsc"] = fetch_gsc()
    print(f"  GSC available={result['gsc']['available']}")

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 寫入 {OUT}")

    render_html(result)


def render_html(result):
    """把資料嵌入 template.html，輸出自包含的 index.html（雙擊即可開）。"""
    template = ROOT / "template.html"
    if not template.exists():
        print("  ⚠ 找不到 template.html，略過 HTML 產生", file=sys.stderr)
        return
    html = template.read_text(encoding="utf-8")
    # 資料轉 JSON；把 </ 轉成 <\/ 以免字串內的 </script> 提前結束內嵌腳本
    cfg_json = json.dumps(CONFIG, ensure_ascii=False).replace("</", "<\\/")
    data_json = json.dumps(result, ensure_ascii=False).replace("</", "<\\/")
    payload = (
        "<script>\n"
        f"window.__CONFIG__ = {cfg_json};\n"
        f"window.__DATA__ = {data_json};\n"
        "</script>"
    )
    html = html.replace("<!--__DATA__-->", payload)
    out = ROOT / "index.html"
    out.write_text(html, encoding="utf-8")
    print(f"✓ 產生 {out}")


if __name__ == "__main__":
    main()
