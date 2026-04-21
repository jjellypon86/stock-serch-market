#!/bin/bash
set -e

echo "=== K-Quant Tracker 환경 설정 ==="

# 가상환경 생성
if [ ! -d "venv" ]; then
    echo "가상환경 생성 중..."
    python3 -m venv venv
fi

# 가상환경 활성화
source venv/bin/activate

# 패키지 설치
echo "패키지 설치 중..."
pip install --upgrade pip -q
pip install -r requirements.txt -q

echo "=== 설치 완료. Streamlit 앱 실행 ==="
streamlit run app.py
