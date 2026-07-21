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

# 本機執行：讀專案根目錄 .env（雲端 GitHub Actions 已由 Secret 直接注入環境變數，無 .env 檔時安全略過）
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

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
if not SA_PATH and (ROOT / "service-account.json").exists():
    SA_PATH = str(ROOT / "service-account.json")
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = SA_PATH  # GA4 client 走 ADC，需靠此環境變數找到憑證檔

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

    # ---- 全期逐日細分：所有維度都改抓「日期 × 維度」的原始明細，
    # 不再由 Python 針對固定 28 天窗預先聚合。聚合／期間比較全部交給前端，
    # 讓使用者選任意日期範圍時，整頁（含來源/裝置/廣告/點擊/分流樞紐）都能重新算。----
    def and_(*ex):
        return FilterExpression(and_group=FilterExpressionList(expressions=list(ex)))

    click_filter = and_(event_eq("content_click"), page_contains())
    lead_filter = and_(event_eq("generate_lead"), page_contains())
    paid = FilterExpression(filter=Filter(
        field_name="sessionMedium",
        string_filter=Filter.StringFilter(match_type=Filter.StringFilter.MatchType.CONTAINS, value="cpc")))
    paid_page = and_(page_contains(), paid)
    paid_lead = and_(page_contains(), paid, event_eq("generate_lead"))

    def ref_contains(val):
        # 後一頁也鎖同網域：落地頁須在 www.digiwin.com.tw（跨站如就享知自然排除）
        return with_host(FilterExpression(filter=Filter(
            field_name="pageReferrer",
            string_filter=Filter.StringFilter(
                match_type=Filter.StringFilter.MatchType.CONTAINS, value=val))))

    def by_day_1d(dim, metric, dim_filter, limit=200000):
        """回傳 {date: {dim值: 指標}}"""
        req = RunReportRequest(
            property=prop,
            date_ranges=[DateRange(start_date=start, end_date=end)],
            dimensions=[Dimension(name="date"), Dimension(name=dim)],
            metrics=[Metric(name=metric)],
            dimension_filter=dim_filter,
            limit=limit,
        )
        d = {}
        for row in client.run_report(req).rows:
            dd = row.dimension_values[0].value
            dd = f"{dd[0:4]}-{dd[4:6]}-{dd[6:8]}"
            k = row.dimension_values[1].value or "(未定義)"
            v = int(float(row.metric_values[0].value))
            d.setdefault(dd, {})[k] = d.get(dd, {}).get(k, 0) + v
        return d

    def by_day_2d(dim1, dim2, metric, dim_filter, limit=200000):
        """回傳 {date: {dim1值: {dim2值: 指標}}}"""
        req = RunReportRequest(
            property=prop,
            date_ranges=[DateRange(start_date=start, end_date=end)],
            dimensions=[Dimension(name="date"), Dimension(name=dim1), Dimension(name=dim2)],
            metrics=[Metric(name=metric)],
            dimension_filter=dim_filter,
            limit=limit,
        )
        d = {}
        for row in client.run_report(req).rows:
            dd = row.dimension_values[0].value
            dd = f"{dd[0:4]}-{dd[4:6]}-{dd[6:8]}"
            k1 = row.dimension_values[1].value or "(未定義)"
            k2 = row.dimension_values[2].value or "(未定義)"
            d.setdefault(dd, {}).setdefault(k1, {})[k2] = int(float(row.metric_values[0].value))
        return d

    def by_day_scalar(metric, dim_filter):
        """回傳 {date: 指標}（無額外維度）"""
        req = RunReportRequest(
            property=prop,
            date_ranges=[DateRange(start_date=start, end_date=end)],
            dimensions=[Dimension(name="date")],
            metrics=[Metric(name=metric)],
            dimension_filter=dim_filter,
            limit=100000,
        )
        d = {}
        for row in client.run_report(req).rows:
            dd = row.dimension_values[0].value
            dd = f"{dd[0:4]}-{dd[4:6]}-{dd[6:8]}"
            d[dd] = int(float(row.metric_values[0].value))
        return d

    by_day = {}
    by_day["source_sessions"] = by_day_1d("sessionDefaultChannelGroup", "sessions", page_contains())
    by_day["source_leads"] = by_day_1d("sessionDefaultChannelGroup", "eventCount", lead_filter)
    by_day["device_users"] = by_day_1d("deviceCategory", "totalUsers", page_contains())
    by_day["device_leads"] = by_day_1d("deviceCategory", "eventCount", lead_filter)
    by_day["click"] = by_day_1d("customEvent:click_text", "eventCount", click_filter)
    by_day["click_device"] = by_day_2d("customEvent:click_text", "deviceCategory", "eventCount", click_filter)
    by_day["prev_raw"] = by_day_1d("pageReferrer", "screenPageViews", page_contains())
    by_day["next_raw"] = by_day_1d("pagePath", "screenPageViews", ref_contains("erp-all"))
    by_day["ads_sessions"] = by_day_scalar("sessions", paid_page)
    by_day["ads_leads"] = by_day_scalar("eventCount", paid_lead)
    by_day["campaign_sessions"] = by_day_1d("sessionCampaignName", "sessions", paid_page)
    by_day["campaign_leads"] = by_day_1d("sessionCampaignName", "eventCount", paid_lead)
    out["by_day"] = by_day

    # 標題對照：直接用 GA4 的 pageTitle（涵蓋全站所有頁，跨整個抓取範圍），titles.json 僅備援
    def path_title_map():
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

    out["titles_ga4"] = path_title_map()

    titles_fallback = {}
    tp = DATA_DIR / "titles.json"
    if tp.exists():
        try:
            titles_fallback = json.loads(tp.read_text(encoding="utf-8"))
        except Exception:
            titles_fallback = {}
    out["titles_fallback"] = titles_fallback

    # 分流樞紐用：erp-all 實際連結白名單（前端據此篩「不是下一頁」的資料）
    wl = fetch_erpall_links()
    out["next_whitelist"] = sorted(wl) if wl else []
    out["whitelist_used"] = wl is not None
    out["industry_keywords"] = INDUSTRY_KEYWORDS
    out["conv_paths"] = ["/contact/eform", "/contact/success"]

    return out


# ============================ GSC ============================
def fetch_gsc():
    # GSC_PROPERTY_URL 可能是逗號分隔的多個資源，逐一嘗試，挑能查到 erp-all 資料的那個
    candidates = [u.strip() for u in GSC_URL.split(",") if u.strip()]
    if not candidates:
        return {"available": False, "note": "未設定 GSC_PROPERTY_URL", "daily": [], "query_by_day": {}}
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_file(
            SA_PATH, scopes=["https://www.googleapis.com/auth/webmasters.readonly"])
        svc = build("searchconsole", "v1", credentials=creds, cache_discovery=False)

        # GSC 資料延遲約 2~3 天：抓「全期逐日／逐日×搜尋字」明細，任意期間比較交給前端
        lag = 3
        today = dt.date.today()
        cur_e = (today - dt.timedelta(days=lag)).isoformat()
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
                probe = raw_query(site, hist_s, cur_e, ["date"], 1)
                chosen = site
                if probe.get("rows"):
                    break
            except Exception as e:
                last_err = e
                continue
        if not chosen:
            return {"available": False,
                    "note": f"所有 GSC 資源查詢皆失敗：{type(last_err).__name__}: {last_err}",
                    "daily": [], "query_by_day": {}}

        def q(s, e, dimensions, limit):
            return raw_query(chosen, s, e, dimensions, limit)

        daily = []
        for row in q(hist_s, cur_e, ["date"], 1000).get("rows", []):
            daily.append({"date": row["keys"][0], "clicks": int(row.get("clicks", 0)),
                          "impressions": int(row.get("impressions", 0)),
                          "ctr": round(row.get("ctr", 0) * 100, 2),
                          "position": round(row.get("position", 0), 1)})

        # 逐日 × 搜尋字：前端依選定範圍加總 clicks/impressions，position 用曝光數加權平均重建
        query_by_day = {}
        for row in q(hist_s, cur_e, ["date", "query"], 25000).get("rows", []):
            d, query = row["keys"][0], row["keys"][1]
            query_by_day.setdefault(d, {})[query] = {
                "clicks": int(row.get("clicks", 0)),
                "impressions": int(row.get("impressions", 0)),
                "position": round(row.get("position", 0), 1),
            }

        return {"available": True, "range": [hist_s, cur_e], "daily": daily, "query_by_day": query_by_day}
    except Exception as e:
        return {"available": False, "note": f"GSC 未授權或抓取失敗：{type(e).__name__}: {e}",
                "daily": [], "query_by_day": {}}


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
    print(f"  GA4 每日資料 {len(result['ga4']['daily'])} 天")

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
