"""
GitHub Actions에서 평일 장마감 후 실행하는 무인 스캔 스크립트.
scanner.py/sheets.py는 Streamlit 앱 전용이 아니라 순수 함수 위주라 그대로 재사용한다.
st.* 호출(progress/warning)은 ScriptRunContext 없이 실행돼도 경고만 찍고 무시되므로
Streamlit 서버 없이 `python scripts/daily_scan.py`로 직접 실행 가능.

필요한 환경: .streamlit/secrets.toml (워크플로에서 STREAMLIT_SECRETS_TOML 시크릿으로 생성)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scanner import scan_day_trading
from sheets import is_configured, save_scan_results, update_results
from utils import get_last_trading_date

MARKETS = ["KOSPI", "KOSDAQ"]


def main() -> None:
    if not is_configured():
        print("[daily_scan] gcp_service_account 시크릿이 설정되지 않았습니다. 중단.")
        sys.exit(1)

    date = get_last_trading_date()
    print(f"[daily_scan] 기준일: {date}")

    for market in MARKETS:
        print(f"[daily_scan] {market} 단기 스캔 시작...")
        try:
            df = scan_day_trading(date, market)
        except Exception as e:
            print(f"[daily_scan] {market} 스캔 실패: {e}")
            continue

        if df.empty:
            print(f"[daily_scan] {market}: 후보 없음")
            continue

        ok, err, saved, skipped = save_scan_results(df, "day", market, date)
        if ok:
            print(f"[daily_scan] {market}: {saved}건 저장, {skipped}건 중복 스킵")
        else:
            print(f"[daily_scan] {market} 저장 실패: {err}")

    print("[daily_scan] 결과 업데이트(WIN/LOSS/EXPIRED 자동 판정) 실행...")
    updated, err = update_results()
    if err:
        print(f"[daily_scan] 결과 업데이트 오류: {err}")
    else:
        print(f"[daily_scan] {updated}건 업데이트 완료")


if __name__ == "__main__":
    main()
