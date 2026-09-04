"""
종목별 실제 보고통화 매핑 테이블

이 테이블은 '자동 제안값'으로만 사용됩니다.
최종 확정 통화는 대시보드(프론트엔드)에서 사용자가 직접 확인/수정하여
Supabase의 companies 테이블에 저장하는 것을 권장합니다.

동방재부(AKShare) API가 반환하는 CURRENCY 필드는:
- A주(상해/선전): 신뢰 가능 (실제 값이 정확히 CNY로 나옴, 검증 완료)
- 홍콩: 신뢰 불가 (실제로는 시스템 기본값 'HKD'가 기계적으로 채워지는 것으로 추정,
        텐센트(00700, 실제 RMB 보고)로 교차검증하여 확인됨)

따라서 홍콩 종목은 아래처럼 회사별로 직접 확인해서 등록해야 합니다.
새 종목 추가 시: 회사의 연차보고서(annual report) 1페이지 통상
"presented in RMB/USD/HKD" 문구를 검색해서 확인 후 아래에 추가하세요.
"""

# 홍콩(.HK) 종목의 실제 재무제표 보고통화
# key: 5자리 홍콩 종목코드 (0 패딩), value: (통화코드, 확인출처 메모)
HK_CURRENCY_MAP = {
    "01801": ("CNY", "이노벤트 바이오 - 2025 연차보고서: 'Net profit... reached RMB813.6 million'"),
    "06160": ("USD", "비원메디슨(BeOne, 옛 베이진) - 2025 연결재무제표: 'product revenue of USD 5.28 billion'"),
    "00700": ("CNY", "텐센트 - 2025 실적발표: 'Revenues... to RMB751.8 billion' (교차검증 완료)"),
    "03696": ("HKD", "인실리코 메디슨 - AKShare 원본 매출(2025, HKD 기준) 395,292,683 / 환율(~7.8) 환산 시 "
                     "실제 공시 매출($56.24M USD, stockanalysis.com)과 일치. AKShare 원본이 HKD로 기록되어 "
                     "있음을 실측으로 확인함(2026-09 검증)."),
    # 아래는 추정치이며 검증 필요 (신규 등록 시 확인 후 verified: True로 변경 권장)
    "00005": ("USD", "HSBC - 추정치, 검증 필요"),
}

# A주(.SH/.SZ) 종목은 AKShare CURRENCY 필드를 그대로 신뢰
# (별도 매핑 불필요, 항상 CNY)
A_SHARE_DEFAULT_CURRENCY = "CNY"


def normalize_hk_code(ticker_or_code: str) -> str:
    """'1801.HK', '1801', '01801' 등 다양한 입력을 5자리 코드로 통일"""
    code = ticker_or_code.upper().replace(".HK", "").strip()
    return code.zfill(5)


def suggest_currency(ticker: str) -> dict:
    """
    종목코드를 받아 자동 제안 통화를 반환.
    반환값은 '제안'일 뿐, 프론트엔드에서 사용자 확인/override를 거치는 것을 전제로 함.
    """
    ticker_upper = ticker.upper()

    if ticker_upper.endswith(".SH") or ticker_upper.endswith(".SZ"):
        return {
            "suggested_currency": A_SHARE_DEFAULT_CURRENCY,
            "confidence": "high",
            "source": "A주는 AKShare CURRENCY 필드가 신뢰 가능하여 자동 확정",
            "needs_user_confirmation": False,
        }

    if ticker_upper.endswith(".HK"):
        code = normalize_hk_code(ticker_upper)
        if code in HK_CURRENCY_MAP:
            currency, note = HK_CURRENCY_MAP[code]
            return {
                "suggested_currency": currency,
                "confidence": "high" if "교차검증" in note or "연차보고서" in note else "medium",
                "source": note,
                "needs_user_confirmation": False,
            }
        else:
            return {
                "suggested_currency": None,
                "confidence": "none",
                "source": "매핑 테이블에 없는 신규 홍콩 종목입니다. 회사 연차보고서를 확인 후 직접 입력해 주세요.",
                "needs_user_confirmation": True,
            }

    return {
        "suggested_currency": None,
        "confidence": "none",
        "source": "인식할 수 없는 티커 형식입니다 (.SH/.SZ/.HK 형식이어야 함)",
        "needs_user_confirmation": True,
    }
