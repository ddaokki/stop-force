#!/usr/bin/env bash
# 실행: bash run.sh
cd "$(dirname "$0")" || exit 1
echo "=== Stop-Force ==="

if [ ! -f .env ]; then
  cp .env.example .env 2>/dev/null || printf 'NVIDIA_API_KEY=\nNVIDIA_MODEL=nvidia/nemotron-3.5-lightning-30b-a3b\n' > .env
  echo "[INFO] .env 파일을 만들었습니다 (숨김 파일이라 ls -a 로 보입니다)."
  echo "       nano .env 로 열어 NVIDIA_API_KEY= 뒤에 키를 넣고 다시 실행하세요."
  exit 0
fi
if grep -q '여기에_키를' .env || grep -qE '^NVIDIA_API_KEY=\s*$' .env; then
  echo "[INFO] .env 에 API 키가 아직 없습니다. (키 없이도 화면의 '데모 모드'는 동작합니다)"
fi

if [ ! -x .venv/bin/python ]; then
  echo "[INFO] 가상환경 생성 중..."
  if ! python3 -m venv .venv; then
    echo "[ERROR] venv 생성 실패. Ubuntu/Debian이면: sudo apt install python3-venv"
    exit 1
  fi
fi

echo "[INFO] 패키지 설치 중..."
.venv/bin/python -m pip install -q -r requirements.txt || { echo "[ERROR] pip 설치 실패"; exit 1; }

echo "[INFO] 웹 앱 실행: http://localhost:8501"
exec .venv/bin/python -m streamlit run app.py
