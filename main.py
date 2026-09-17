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
                "revenue": _to_millions(_safe_float(row.get("TOTAL_OPERATE_INCOME"))),
                "operating_income": _to_millions(_safe_float(row.get("OPERATE_PROFIT"))),
                "operating_income_is_gross_profit_fallback": False,
                "net_income": _to_millions(_safe_float(row.get("PARENT_NETPROFIT"))),
            })

        return {
            "ticker": ticker,
            "market": "A-share",
            "currency": currency,
            "currency_confirmed": True,
            "frequency": freq,
            "unit": "백만 (million)",
            "data": data,
        }

    else:  # HK
        code = normalize_hk_code(ticker)

        if freq == "annual":
            try:
                df = ak.stock_financial_hk_report_em(stock=code, symbol="利润表", indicator="年度")
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"AKShare 조회 실패: {e}")

            if df is None or df.empty:
                raise HTTPException(status_code=404, detail="데이터가 없습니다")

            report_dates = sorted(df["REPORT_DATE"].unique(), reverse=True)[:limit]
            data = []
            for rd in report_dates:
                sub = df[df["REPORT_DATE"] == rd]
                m = _extract_hk_metrics(sub)
                data.append(_build_period_entry(str(rd)[:10], f"{str(rd)[:4]}년", m["revenue"], m["operating_income"],
                                                 m["gross_profit"], m["net_income"]))

        else:
            # ⚠ 홍콩거래소는 분기 공시 의무가 없고 반기(H1)/연간(FY) 공시만 함.
            # AKShare의 "报告期"는 H1(6/30, 상반기 누적)과 FY(12/31, 연간 누적) 값을
            # 그대로 섞어서 주기 때문에, 하반기(H2) 실적을 보려면
            # H2 = FY(연간 누적) - H1(상반기 누적) 로 직접 계산해야 함.
            try:
                df = ak.stock_financial_hk_report_em(stock=code, symbol="利润表", indicator="报告期")
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"AKShare 조회 실패: {e}")

            if df is None or df.empty:
                raise HTTPException(status_code=404, detail="데이터가 없습니다")

            by_year = {}
            for rd in df["REPORT_DATE"].unique():
                rd_str = str(rd)[:10]
                year, month_day = rd_str[:4], rd_str[5:10]
                sub = df[df["REPORT_DATE"] == rd]
                m = _extract_hk_metrics(sub)
                by_year.setdefault(year, {})
                if month_day == "06-30":
                    by_year[year]["h1"] = m
                elif month_day == "12-31":
                    by_year[year]["fy"] = m

            entries = []  # (정렬용 종료일, period_entry)
            for year, parts in by_year.items():
                h1, fy = parts.get("h1"), parts.get("fy")
                if h1:
                    entries.append((f"{year}-06-30", _build_period_entry(
                        f"{year}-06-30", f"{year} 상반기",
                        h1["revenue"], h1["operating_income"], h1["gross_profit"], h1["net_income"])))
                if fy:
                    if h1:
                        h2_rev = _diff(fy["revenue"], h1["revenue"])
                        h2_op = _diff(fy["operating_income"], h1["operating_income"])
                        h2_gp = _diff(fy["gross_profit"], h1["gross_profit"])
                        h2_net = _diff(fy["net_income"], h1["net_income"])
                        entries.append((f"{year}-12-31", _build_period_entry(
                            f"{year}-12-31", f"{year} 하반기", h2_rev, h2_op, h2_gp, h2_net)))
                    else:
                        # 상반기 데이터 없이 연간만 있는 예외 케이스 -> 연간 값을 그대로 표기(하반기 아님을 명시)
                        entries.append((f"{year}-12-31", _build_period_entry(
                            f"{year}-12-31", f"{year} 연간(상반기 데이터 없음)",
                            fy["revenue"], fy["operating_income"], fy["gross_profit"], fy["net_income"])))

            entries.sort(key=lambda x: x[0], reverse=True)
            data = [e[1] for e in entries[:limit]]

        # 통화는 매핑 테이블 제안값 사용 (신뢰 가능한 필드가 API에 없으므로)
        suggestion = suggest_currency(ticker)

        return {
            "ticker": ticker,
            "market": "HK",
            "currency": suggestion["suggested_currency"],
            "currency_confirmed": not suggestion["needs_user_confirmation"],
            "currency_note": suggestion["source"],
            "frequency": freq,
            "unit": "백만 (million)",
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
    market_cap_100m = _last_value(cap_df)  # Baidu 원본 단위: 억(100 million)
    market_cap = round(market_cap_100m * 100, 2) if market_cap_100m is not None else None  # -> 백만 단위로 환산

    # PSR 계산을 위해 최근 연매출 조회 (통화 일치 여부 확인 필요)
    # /financials가 이미 매출을 '백만' 단위로 반환하므로, 시가총액도 백만 단위로 맞추면
    # 단순히 market_cap / revenue 로 바로 계산 가능 (억->원 환산 등 불필요)
    fin = get_financials(ticker=ticker, freq="annual", limit=1)
    revenue = fin["data"][0]["revenue"] if fin["data"] else None  # 이미 백만 단위
    revenue_currency = fin["currency"]

    psr = None
    psr_note = None
    if market_cap is not None and revenue not in (None, 0):
        if revenue_currency == market_cap_currency:
            psr = round(market_cap / revenue, 2)
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
        "market_cap_unit": "백만 (million)",
        "market_cap_currency": market_cap_currency,
        "psr": psr,
        "psr_note": psr_note,
        "revenue_currency": revenue_currency,
    }


# ----------------------------------------------------------------------
# 4) 현재 시세 (주가·시가총액·등락률) — iTick 대체용
# ----------------------------------------------------------------------
@app.get("/quote")
def get_quote(ticker: str = Query(..., description="예: 600519.SH, 0700.HK")):
    """
    동방재부(东方财富) 전종목 스팟시세 스냅샷에서 해당 종목만 필터링.
    ⚠ 전체 시장을 한 번에 받아오는 방식이라 개별 종목 API보다 응답이 약간
    느릴 수 있으나, 별도 계정·API 키·만료 문제 없이 완전 무료로 지속 사용 가능.
    """
    market = detect_market(ticker)

    try:
        if market == "HK":
            df = ak.stock_hk_spot_em()
            code = normalize_hk_code(ticker)
        else:
            df = ak.stock_zh_a_spot_em()
            code = ticker.upper().replace(".SH", "").replace(".SZ", "")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AKShare 시세 조회 실패: {e}")

    col_code = next((c for c in df.columns if c == "代码" or "代码" in c), None)
    if col_code is None:
        raise HTTPException(status_code=502, detail=f"종목코드 컬럼을 찾지 못함. 실제 컬럼: {list(df.columns)}")

    row = df[df[col_code].astype(str).str.strip() == code]
    if row.empty:
        raise HTTPException(status_code=404, detail=f"'{code}' 종목을 시세 스냅샷에서 찾지 못했습니다")
    row = row.iloc[0]

    col_price = next((c for c in df.columns if "最新价" in c), None)
    col_change_pct = next((c for c in df.columns if "涨跌幅" in c), None)
    col_market_cap = next((c for c in df.columns if "总市值" in c), None)

    price = _safe_float(row.get(col_price)) if col_price else None
    change_pct = _safe_float(row.get(col_change_pct)) if col_change_pct else None
    market_cap_raw = _safe_float(row.get(col_market_cap)) if col_market_cap else None
    market_cap = _to_millions(market_cap_raw) if market_cap_raw is not None else None

    return {
        "ticker": ticker,
        "price": price,
        "change_pct": change_pct,
        "market_cap": market_cap,
        "market_cap_unit": "백만 (million)" if market_cap is not None else None,
        "market_cap_currency": "HKD" if market == "HK" else A_SHARE_DEFAULT_CURRENCY,
        # 진단용 - 실제 컬럼명이 다를 경우 확인하기 위함. 안정화되면 제거 가능.
        "_debug_columns": list(df.columns)[:15],
    }


# ----------------------------------------------------------------------
# 5) 1년치 일별 시세 히스토리 — iTick kline 대체용
# ----------------------------------------------------------------------
@app.get("/history")
def get_history(
    ticker: str = Query(..., description="예: 600519.SH, 0700.HK"),
    days: int = Query(365, ge=30, le=1500),
):
    from datetime import datetime, timedelta

    market = detect_market(ticker)
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")

    try:
        if market == "HK":
            code = normalize_hk_code(ticker)
            df = ak.stock_hk_hist(symbol=code, period="daily", start_date=start_date, end_date=end_date, adjust="")
        else:
            code = ticker.upper().replace(".SH", "").replace(".SZ", "")
            df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start_date, end_date=end_date, adjust="")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AKShare 히스토리 조회 실패: {e}")

    if df is None or df.empty:
        return {"ticker": ticker, "data": []}

    col_date = next((c for c in df.columns if "日期" in c), None)
    col_close = next((c for c in df.columns if c == "收盘" or "收盘" in c), None)
    if col_date is None or col_close is None:
        raise HTTPException(status_code=502, detail=f"필요 컬럼을 찾지 못함. 실제 컬럼: {list(df.columns)}")

    data = [
        {"date": str(row[col_date])[:10], "close": _safe_float(row[col_close])}
        for _, row in df.iterrows()
    ]
    return {"ticker": ticker, "data": data}


# ----------------------------------------------------------------------
# 4) 개별 종목 뉴스
# ----------------------------------------------------------------------
@app.get("/news")
def get_news(
    ticker: str = Query(..., description="예: 600519.SH, 0700.HK"),
    limit: int = Query(10, ge=1, le=50),
):
    """
    동방재부(东方财富) 뉴스 검색 기반.
    종목코드로 검색하면 무관한 결과가 많이 섞여서(예: 다른 종목의 코드가 우연히
    일치), 먼저 회사의 실제 중국어 이름을 조회한 뒤 그 이름으로 검색합니다.
    """
    market = detect_market(ticker)
    company_name = _get_company_name(ticker, market)
    search_keyword = company_name or ticker.upper().replace(".SH", "").replace(".SZ", "").replace(".HK", "")

    try:
        df = ak.stock_news_em(symbol=search_keyword)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AKShare 뉴스 조회 실패: {e}")

    if df is None or df.empty:
        return {"ticker": ticker, "company_name": company_name, "data": []}

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

    return {"ticker": ticker, "company_name": company_name, "data": data}


def _get_company_name(ticker: str, market: str):
    """이미 재무제표 조회에 쓰는 함수의 SECURITY_NAME_ABBR 필드를 재사용해
    회사의 실제 중국어 이름을 가져옴 (뉴스 검색 정확도를 위해 필요)."""
    try:
        if market in ("A_SH", "A_SZ"):
            symbol = a_share_em_symbol(ticker)
            df = ak.stock_profit_sheet_by_yearly_em(symbol=symbol)
        else:
            code = normalize_hk_code(ticker)
            df = ak.stock_financial_hk_report_em(stock=code, symbol="利润表", indicator="年度")
        if df is not None and not df.empty and "SECURITY_NAME_ABBR" in df.columns:
            return df["SECURITY_NAME_ABBR"].iloc[0]
    except Exception:
        pass
    return None


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


def _extract_hk_metrics(sub: pd.DataFrame) -> dict:
    """홍콩 손익계산서 한 보고기간(sub)에서 필요한 4개 항목을 원 단위(raw)로 추출"""
    return {
        "revenue": _extract_item(sub, "营业额"),
        "operating_income": _extract_item(sub, "经营溢利"),
        "gross_profit": _extract_item(sub, "毛利"),
        "net_income": _extract_item(sub, "股东应占溢利"),
    }


def _diff(a, b):
    """a-b. 둘 중 하나라도 없으면 None (섣불리 잘못된 값을 만들지 않기 위함)"""
    if a is None or b is None:
        return None
    return a - b


def _to_millions(v):
    if v is None:
        return None
    return round(v / 1_000_000, 2)


def _build_period_entry(period: str, period_label: str, revenue, operating_income, gross_profit, net_income) -> dict:
    """반기/연간 raw 금액을 받아 백만 단위로 환산 + 영업이익 없으면 매출총이익 대체 표시하는 공통 로직"""
    is_fallback = operating_income is None and gross_profit is not None
    final_op = operating_income if operating_income is not None else gross_profit
    return {
        "period": period,
        "period_label": period_label,
        "revenue": _to_millions(revenue),
        "operating_income": _to_millions(final_op),
        "operating_income_is_gross_profit_fallback": is_fallback,
        "net_income": _to_millions(net_income),
    }


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
                      "/news?ticker=0700.HK",
                      "/quote?ticker=0700.HK",
                      "/history?ticker=0700.HK&days=365"],
    }
