"""
AKShare 프록시 서버
- 홍콩(.HK) / 상해(.SH) / 선전(.SZ) 종목의 재무제표·밸류에이션을 통일된 JSON으로 제공
- 정적 대시보드(GitHub Pages)에서 브라우저 fetch()로 직접 호출 가능하도록 CORS 허용

배포: Render, Railway 등에 이 폴더 전체를 올리면 됨
로컬 실행: uvicorn main:app --reload --port 8000
"""

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import akshare as ak
import pandas as pd

from currency_mapping import suggest_currency, normalize_hk_code, A_SHARE_DEFAULT_CURRENCY

app = FastAPI(title="China/HK Stock Data Proxy", version="1.0")

# 브라우저(GitHub Pages 등)에서 직접 fetch() 가능하도록 모든 origin 허용
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def detect_market(ticker: str) -> str:
    t = ticker.upper()
    if t.endswith(".SH"):
        return "A_SH"
    if t.endswith(".SZ"):
        return "A_SZ"
    if t.endswith(".HK"):
        return "HK"
    raise HTTPException(status_code=400, detail="티커는 .SH, .SZ, .HK 중 하나로 끝나야 합니다 (예: 600519.SH, 000001.SZ, 0700.HK)")


def a_share_em_symbol(ticker: str) -> str:
    """'600519.SH' -> 'SH600519' 형식으로 변환 (동방재부 함수가 요구하는 형식)"""
    code = ticker.upper().replace(".SH", "").replace(".SZ", "")
    market = "SH" if ticker.upper().endswith(".SH") else "SZ"
    return f"{market}{code}"


# ----------------------------------------------------------------------
# 1) 통화 제안 엔드포인트
# ----------------------------------------------------------------------
@app.get("/currency-suggestion")
def currency_suggestion(ticker: str = Query(..., description="예: 1801.HK, 600519.SH")):
    """
    종목의 실제 보고통화를 자동 제안.
    프론트엔드는 이 값을 '기본값'으로 보여주되, 사용자가 직접 수정/확정하여
    Supabase 등 자체 DB에 최종 저장하는 것을 권장.
    """
    return suggest_currency(ticker)


# ----------------------------------------------------------------------
# 2) 손익계산서 (매출/영업이익(또는 매출총이익)/순이익)
# ----------------------------------------------------------------------
@app.get("/financials")
def get_financials(
    ticker: str = Query(..., description="예: 600519.SH, 000001.SZ, 0700.HK"),
    freq: str = Query("annual", pattern="^(annual|quarterly)$"),
    limit: int = Query(5, ge=1, le=20),
):
    market = detect_market(ticker)

    if market in ("A_SH", "A_SZ"):
        symbol = a_share_em_symbol(ticker)
        try:
            if freq == "annual":
                df = ak.stock_profit_sheet_by_yearly_em(symbol=symbol)
            else:
                df = ak.stock_profit_sheet_by_quarterly_em(symbol=symbol)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"AKShare 조회 실패: {e}")

        if df is None or df.empty:
            raise HTTPException(status_code=404, detail="데이터가 없습니다")

        df = df.head(limit)
        currency = df["CURRENCY"].iloc[0] if "CURRENCY" in df.columns else A_SHARE_DEFAULT_CURRENCY

        data = []
        for _, row in df.iterrows():
            data.append({
                "period": str(row.get("REPORT_DATE"))[:10],
                "period_label": row.get("REPORT_DATE_NAME"),
                "revenue": _safe_float(row.get("TOTAL_OPERATE_INCOME")),
                "operating_income": _safe_float(row.get("OPERATE_PROFIT")),
                "operating_income_is_gross_profit_fallback": False,
                "net_income": _safe_float(row.get("PARENT_NETPROFIT")),
            })

        return {
            "ticker": ticker,
            "market": "A-share",
            "currency": currency,
            "currency_confirmed": True,
            "frequency": freq,
            "data": data,
        }

    else:  # HK
        code = normalize_hk_code(ticker)
        indicator = "年度" if freq == "annual" else "报告期"
        try:
            df = ak.stock_financial_hk_report_em(stock=code, symbol="利润表", indicator=indicator)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"AKShare 조회 실패: {e}")

        if df is None or df.empty:
            raise HTTPException(status_code=404, detail="데이터가 없습니다")

        # 보고기간(REPORT_DATE) 단위로 그룹화 -> 각 기간별 필요한 항목만 추출
        report_dates = df["REPORT_DATE"].unique()
        report_dates = sorted(report_dates, reverse=True)[:limit]

        data = []
        for rd in report_dates:
            sub = df[df["REPORT_DATE"] == rd]
            revenue = _extract_item(sub, "营业额")
            operating_income = _extract_item(sub, "经营溢利")
            gross_profit = _extract_item(sub, "毛利")
            net_income = _extract_item(sub, "股东应占溢利")

            is_fallback = operating_income is None
            data.append({
                "period": str(rd)[:10],
                "period_label": None,
                "revenue": revenue,
                "operating_income": operating_income if operating_income is not None else gross_profit,
                "operating_income_is_gross_profit_fallback": is_fallback,
                "net_income": net_income,
            })

        # 통화는 매핑 테이블 제안값 사용 (신뢰 가능한 필드가 API에 없으므로)
        suggestion = suggest_currency(ticker)

        return {
            "ticker": ticker,
            "market": "HK",
            "currency": suggestion["suggested_currency"],
            "currency_confirmed": not suggestion["needs_user_confirmation"],
            "currency_note": suggestion["source"],
            "frequency": freq,
            "data": data,
        }


# ----------------------------------------------------------------------
# 3) 밸류에이션 (PER, PBR, 시가총액, PSR)
# ----------------------------------------------------------------------
@app.get("/valuation")
def get_valuation(ticker: str = Query(..., description="예: 600519.SH, 0700.HK")):
    market = detect_market(ticker)

    try:
        if market in ("A_SH", "A_SZ"):
            code = ticker.upper().replace(".SH", "").replace(".SZ", "")
            per_df = ak.stock_zh_valuation_baidu(symbol=code, indicator="市盈率(TTM)", period="近一年")
            pbr_df = ak.stock_zh_valuation_baidu(symbol=code, indicator="市净率", period="近一年")
            cap_df = ak.stock_zh_valuation_baidu(symbol=code, indicator="总市值", period="近一年")
            market_cap_currency = A_SHARE_DEFAULT_CURRENCY
        else:
            code = normalize_hk_code(ticker)
            per_df = ak.stock_hk_valuation_baidu(symbol=code, indicator="市盈率(TTM)", period="近一年")
            pbr_df = ak.stock_hk_valuation_baidu(symbol=code, indicator="市净率", period="近一年")
            cap_df = ak.stock_hk_valuation_baidu(symbol=code, indicator="总市值", period="近一年")
            # 홍콩 종목의 시가총액은 실제 거래되는 카운터 기준 통화(대부분 HKD)로 봄
            market_cap_currency = "HKD"
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AKShare 조회 실패: {e}")

    per = _last_value(per_df)
    pbr = _last_value(pbr_df)
    market_cap = _last_value(cap_df)  # 단위: 억(100 million), Baidu 관례

    # PSR 계산을 위해 최근 연매출 조회 (통화 일치 여부 확인 필요)
    fin = get_financials(ticker=ticker, freq="annual", limit=1)
    revenue = fin["data"][0]["revenue"] if fin["data"] else None
    revenue_currency = fin["currency"]

    psr = None
    psr_note = None
    if market_cap is not None and revenue not in (None, 0):
        if revenue_currency == market_cap_currency:
            # market_cap 단위(억) -> 원 단위로 환산 후 매출과 나눔
            psr = round((market_cap * 1e8) / revenue, 2)
        else:
            psr_note = (
                f"통화 불일치로 자동 계산 보류 "
                f"(시가총액: {market_cap_currency}, 매출: {revenue_currency}). "
                f"환율 변환 후 계산 필요."
            )

    return {
        "ticker": ticker,
        "per_ttm": per,
        "pbr": pbr,
        "market_cap": market_cap,
        "market_cap_unit": "억(100 million)",
        "market_cap_currency": market_cap_currency,
        "psr": psr,
        "psr_note": psr_note,
        "revenue_currency": revenue_currency,
    }


# ----------------------------------------------------------------------
# 4) 개별 종목 뉴스
# ----------------------------------------------------------------------
@app.get("/news")
def get_news(
    ticker: str = Query(..., description="예: 600519.SH, 0700.HK"),
    limit: int = Query(10, ge=1, le=50),
):
    """
    동방재부(东方财富) 뉴스 검색 기반. 종목코드 키워드 검색 방식이라
    관련성이 100% 정확하지 않을 수 있음(특히 홍콩 종목).
    """
    market = detect_market(ticker)
    code = ticker.upper().replace(".SH", "").replace(".SZ", "").replace(".HK", "")

    try:
        df = ak.stock_news_em(symbol=code)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AKShare 뉴스 조회 실패: {e}")

    if df is None or df.empty:
        return {"ticker": ticker, "data": []}

    df = df.head(limit)

    # 컬럼명은 AKShare 버전에 따라 다를 수 있어 유연하게 매핑
    col_title = next((c for c in df.columns if "标题" in c), None)
    col_time = next((c for c in df.columns if "时间" in c or "日期" in c), None)
    col_url = next((c for c in df.columns if "链接" in c or "网址" in c), None)
    col_source = next((c for c in df.columns if "来源" in c), None)
    col_content = next((c for c in df.columns if "内容" in c or "摘要" in c), None)

    data = []
    for _, row in df.iterrows():
        data.append({
            "title": row.get(col_title) if col_title else None,
            "time": str(row.get(col_time)) if col_time else None,
            "url": row.get(col_url) if col_url else None,
            "source": row.get(col_source) if col_source else None,
            "summary": row.get(col_content) if col_content else None,
        })

    return {"ticker": ticker, "data": data}


# ----------------------------------------------------------------------
# 유틸 함수
# ----------------------------------------------------------------------
def _safe_float(v):
    try:
        if pd.isna(v):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _extract_item(df: pd.DataFrame, item_name: str):
    """홍콩 손익계산서(long format)에서 STD_ITEM_NAME으로 값 추출"""
    row = df[df["STD_ITEM_NAME"] == item_name]
    if row.empty:
        return None
    return _safe_float(row["AMOUNT"].iloc[0])


def _last_value(df: pd.DataFrame):
    if df is None or df.empty:
        return None
    return _safe_float(df["value"].iloc[-1])


@app.get("/")
def root():
    return {
        "status": "ok",
        "endpoints": ["/financials?ticker=600519.SH&freq=annual",
                      "/valuation?ticker=0700.HK",
                      "/currency-suggestion?ticker=1801.HK",
                      "/news?ticker=0700.HK"],
    }
