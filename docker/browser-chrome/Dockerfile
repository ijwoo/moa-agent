# 실험용 이미지(2026-11-16, D86): 같은 컨테이너에 구글이 내는 크롬(ARM64 리눅스용 공식 .deb)을 더한 것.
# 목적: 브라우저의 종류만 바꿔(Chromium → 크롬) 나머지 조건을 그대로 두고 견주기. 자동화 표시는 그대로다.
FROM local-ai-agent/browser:0.2
USER root
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL -o /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_arm64.deb \
 && apt-get install -y --no-install-recommends /tmp/chrome.deb \
 && rm -f /tmp/chrome.deb && rm -rf /var/lib/apt/lists/*
USER pwuser
WORKDIR /home/pwuser
