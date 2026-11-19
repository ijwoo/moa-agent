# 작업실(D96): 모델이 만든 명령을 실행하는 격리된 방. 비밀은 넣지 않는다 — 이미지에도, 실행 때도.
# 같은 이미지로 나가는 문(egress_proxy.py)도 띄운다.
FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-noto-cjk fonts-nanum ca-certificates chromium \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pillow==11.3.0 numpy==2.3.4 matplotlib==3.10.7 edge-tts==7.2.3 requests==2.32.5

COPY egress_proxy.py /opt/workroom/egress_proxy.py
# 화면 시안(D104): 모델이 쓴 HTML을 폰 크기로 찍는다. 브라우저는 이 방 안의 파일만 연다(사이트 일은 여전히 작업실 밖)
COPY shot.sh /usr/local/bin/shot

RUN useradd --create-home --uid 1100 worker \
 && mkdir -p /work && chown worker:worker /work
USER worker
WORKDIR /work
ENV HOME=/home/worker MPLCONFIGDIR=/tmp/mpl PYTHONDONTWRITEBYTECODE=1 XDG_CONFIG_HOME=/tmp/xdg XDG_CACHE_HOME=/tmp/xdg
