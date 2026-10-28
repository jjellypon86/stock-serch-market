"""
주간 자동 리포트 — 스캔 상태, 눌림목 전략 성과(버전별), 모의추적 현황을 마크다운으로 요약한다.
GitHub Actions(weekly_report.yml)가 실행해 결과를 Issue로 올리므로, 사용자가 로그·앱을 열지 않아도 된다.

사용: python scripts/weekly_report.py <출력 파일 경로>
필요한 환경: .streamlit/secrets.toml (워크플로에서 STREAMLIT_SECRETS_TOML 시크릿으로 생성)
"""

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paper import load_paper_history, summarize_paper
from sheets import is_configured, load_history

STALE_BUSINESS_DAYS = 3  # 마지막 기록이 이 영업일 수 이상 오래되면 경고
DONE_RESULTS = ("WIN", "LOSS", "EXPIRED")


def _last_record_line(label: str, df: pd.DataFrame, today: str) -> str:
    """시트의 마지막 scan_date와 오늘 사이 영업일 수로 '조용히 멈춘 자동화'를 잡아낸다"""
    if df.empty or "scan_date" not in df.columns:
        return f"- {label}: 기록 없음 ⚠️ (시트 접근 실패이거나 아직 기록이 없습니다)"
    last = max(str(x).strip() for x in df["scan_date"] if str(x).strip())
    last_iso = f"{last[:4]}-{last[4:6]}-{last[6:]}"
    gap = int(np.busday_count(last_iso, today))
    warn = f" ⚠️ {gap}영업일째 새 기록이 없습니다" if gap >= STALE_BUSINESS_DAYS else ""
    return f"- {label}: 마지막 기록일 {last_iso}{warn}"


def _history_table(df: pd.DataFrame) -> list[str]:
    if df.empty or "result" not in df.columns:
        return ["- 기록 없음"]
    d = df.copy()
    if "strategy_ver" not in d.columns:
        d["strategy_ver"] = ""
    d["strategy_ver"] = d["strategy_ver"].astype(str).replace("", "미표기")
    d["profit_pct"] = pd.to_numeric(d["profit_pct"], errors="coerce")
    lines = [
        "| 버전 | 완료 | WIN | LOSS | 만료 | 대기 | 승률(%) | 평균수익률(%) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for ver, group in sorted(d.groupby("strategy_ver"), key=lambda kv: kv[0], reverse=True):
        done = group[group["result"].isin(DONE_RESULTS)]
        n = len(done)
        wins = int((done["result"] == "WIN").sum())
        losses = int((done["result"] == "LOSS").sum())
        expired = int((done["result"] == "EXPIRED").sum())
        win_rate = f"{wins / n * 100:.1f}" if n else "-"
        avg = f"{done['profit_pct'].mean():.2f}" if n else "-"
        lines.append(f"| {ver} | {n} | {wins} | {losses} | {expired} | {len(group) - n} | {win_rate} | {avg} |")
    return lines


def _paper_lines(df: pd.DataFrame) -> list[str]:
    stats = summarize_paper(df)
    if not stats:
        return ["- 기록 없음"]
    lines = [f"- 총 추적 {int(stats['총 추적'])}건 · 판정 완료 {int(stats['판정 완료'])}건 · 대기 {int(stats['대기'])}건"]
    if stats["판정 완료"] > 0:
        t_val = stats["t값(28일 블록)"]
        t_text = "표본 블록 5개 미만이라 미계산" if pd.isna(t_val) else f"{t_val}"
        lines += [
            f"- 평균 수익률 {stats['평균 수익률(%)']}% · 같은 기간 시장지수 {stats['평균 시장지수(%)']}%",
            f"- 비용 반영 평균 초과수익 **{stats['평균 비용후 초과(%)']}%** · 초과 플러스 비율 {stats['초과 플러스 비율(%)']}% · t값(28일 블록) {t_text}",
            "- 판단 기준(사전 고정): 비용후 초과 > 0, t값 ≥ 2, 표본 30건 이상, 최소 6개월 관찰. 기준 충족 전에는 실매매 금지.",
        ]
    else:
        lines.append("- 아직 20거래일이 지난 후보가 없어 판정 전입니다 (첫 판정은 2026-11-26 무렵).")
    return lines


def main() -> None:
    if not is_configured():
        print("[weekly_report] gcp_service_account 시크릿이 설정되지 않았습니다. 중단.")
        sys.exit(1)
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("report.md")

    today_dt = datetime.now(ZoneInfo("Asia/Seoul"))
    today = today_dt.strftime("%Y-%m-%d")
    history = load_history()
    paper = load_paper_history()

    report: list[str] = [
        f"@jjellypon86 주간 자동 리포트 ({today} 기준)",
        "",
        "## 1. 자동화 상태",
        _last_record_line("눌림목 스캔(history 시트)", history, today),
        _last_record_line("모의추적(paper_lowvol 시트)", paper, today),
        "- 후보가 0건인 날은 정상일 수 있습니다. 위 경고는 '기록 자체가 며칠째 없을 때'만 표시됩니다.",
        "",
        "## 2. 눌림목 전략 성과 (버전별)",
        *_history_table(history),
        "",
        "## 3. 모의추적 (저변동·고점근접·추세, 20거래일, 실매매 없음)",
        *_paper_lines(paper),
        "",
        "---",
        "자동 생성: `.github/workflows/weekly_report.yml` (매주 토요일 오전 9시 KST). 앱의 '📈 검증', '📡 모의추적' 탭에서 상세 내용을 볼 수 있습니다.",
    ]
    out_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"[weekly_report] 리포트 작성 완료: {out_path}")


if __name__ == "__main__":
    main()
