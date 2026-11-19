# 브라우저 샌드박스 이미지 (Phase 3)
# 베이스 이미지 버전과 Python playwright 버전은 반드시 같아야 한다 (브라우저 실행 파일 경로가 버전에 묶임).
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

# 베이스 이미지에는 브라우저만 있고 Python 패키지는 없다. python3-venv가 없어 전역 pip로 설치한다
# (컨테이너 전용 이미지라 시스템 Python을 오염시켜도 영향 범위가 이 이미지에 한정됨).
RUN pip install --no-cache-dir --break-system-packages playwright==1.64.1

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# 사용자별 브라우저 프로필 볼륨을 붙일 자리 (Phase 5). 빈 볼륨은 이 디렉터리의 소유자(pwuser)를 물려받는다.
RUN mkdir -p /data/profile && chown pwuser:pwuser /data/profile && chmod 700 /data/profile

# 비특권 사용자로만 실행 (root면 Chromium 샌드박스가 꺼진다)
USER pwuser
WORKDIR /home/pwuser
