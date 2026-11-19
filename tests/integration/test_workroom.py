"""작업실 격리 시험(D96, docs/design/workroom.md "지키는 선"). RUN_INTEGRATION=1 + colima 필요.

바깥 접속은 공개 시험 주소(example.com) 하나만 연다(나가는 문이 공인 주소를 잇는지 보려고).
"""

import os
import shutil
import uuid

import pytest

from agent.workroom import manager
from agent.workroom.manager import open_project

pytestmark = pytest.mark.skipif(os.environ.get("RUN_INTEGRATION") != "1", reason="RUN_INTEGRATION=1일 때만")


@pytest.fixture(scope="module")
def room():
    name = f"test-{uuid.uuid4().hex[:8]}"
    w = open_project(name).start()
    yield w
    w.close()
    shutil.rmtree(w.path, ignore_errors=True)


def run(room, cmd, t=30):
    return room.run(cmd, t)


def test_runs_as_an_unprivileged_user_on_a_read_only_system(room):
    r = run(room, "id -u; grep CapEff /proc/self/status; touch /usr/x 2>&1; touch /work/ok && echo work_ok")
    assert r.output.splitlines()[0] == "1100"
    assert "0000000000000000" in r.output                    # 권한(capability) 없음
    assert "Read-only file system" in r.output and "work_ok" in r.output
    assert (room.path / "ok").exists()                       # /work만 맥의 그 프로젝트 폴더와 이어진다


def test_no_secrets_and_only_the_project_folder_is_mounted(room):
    r = run(room, "env; echo ---; cat /proc/mounts")
    env, mounts = r.output.split("---", 1)
    names = {l.split("=", 1)[0] for l in env.splitlines() if "=" in l}
    assert names <= {"PATH", "HOME", "LANG", "MPLCONFIGDIR", "PYTHONDONTWRITEBYTECODE", "HTTP_PROXY", "HTTPS_PROXY",
                     "http_proxy", "https_proxy", "PWD", "SHLVL", "_"}
    binds = [l.split()[1] for l in mounts.splitlines() if len(l.split()) > 1 and l.split()[1].startswith(("/work", "/Users", "/home/worker/.", "/var/run"))]
    assert binds == ["/work"]
    assert "docker.sock" not in mounts


def test_no_direct_way_out_and_the_gate_refuses_private_addresses(room):
    r = run(room, "python3 -c \"import socket;s=socket.socket();s.settimeout(4);print(s.connect_ex(('1.1.1.1',443)))\"")
    assert r.output.strip() != "0"                           # 내부망: 프록시를 거치지 않으면 나갈 수 없다
    probe = ("python3 - <<'E'\nimport requests\n"
             "for u in ['http://host.docker.internal:8765/','https://192.168.5.2/','https://192.168.0.1/','https://10.0.0.1/',"
             "'https://100.100.100.100/','https://127.0.0.1/','http://localhost/','https://169.254.169.254/','https://example.com:8443/']:\n"
             "  try: print('OPEN', u, requests.get(u, timeout=8).status_code)\n"
             "  except Exception as e: print('SHUT', u)\nE")
    r = run(room, probe, 120)
    rows = [l.split()[0] for l in r.output.splitlines() if l.startswith(("OPEN", "SHUT"))]
    assert len(rows) == 9
    assert all(l.startswith("SHUT") or " 403" in l for l in r.output.splitlines() if l.startswith(("OPEN", "SHUT")))
    log = room.gate_log()
    assert "denied_private" in log and "denied_port" in log and " ok" not in log.split("ready", 1)[1].replace("example.com:443 ok", "")


def test_the_gate_opens_public_https(room):
    r = run(room, "python3 -c \"import requests;print(requests.get('https://example.com', timeout=20).status_code)\"", 60)
    assert r.output.strip().endswith("200")


def test_limits_cut_runaway_commands(room):
    r = room.run("sleep 30", 2)
    assert r.timed_out and r.seconds < 10
    r = run(room, "python3 -c \"b=bytearray(3*1024**3); print(len(b))\"", 60)   # 메모리 2GB 상한
    assert r.exit_code != 0
    r = run(room, "python3 -c \"print('가'*50000)\"")
    assert r.truncated and len(r.output) < manager.MAX_OUTPUT + 200


def test_media_tools_work_offline(room):
    r = run(room, "ffmpeg -v error -f lavfi -i color=c=navy:s=1080x1920:d=2 -vf \"drawtext=fontfile=/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf:"
                  "text='작업실':fontcolor=white:fontsize=96:x=(w-tw)/2:y=(h-th)/2\" -r 30 -pix_fmt yuv420p /work/t.mp4 && "
                  "ffprobe -v error -show_entries stream=width,height -of csv=p=0 /work/t.mp4", 120)
    assert r.exit_code == 0 and "1080,1920" in r.output and (room.path / "t.mp4").stat().st_size > 1000


def test_screen_mockups_render_from_html(room):
    # 화면 시안(D104): 작업실 안의 HTML을 폰 크기 PNG로 찍는다 — 비루트·권한 0·읽기 전용 그대로
    (room.path / "m.html").write_text("<!doctype html><meta charset=utf-8><body style='margin:0;background:#111;color:#fff;font:28px sans-serif'>"
                                      "<p style='padding:40px'>보고 싶은 것을 말하면</p></body>", encoding="utf-8")
    r = run(room, "shot m.html m.png && python3 -c \"from PIL import Image;print(Image.open('m.png').size)\"", 120)
    assert r.exit_code == 0 and "(1170, 2532)" in r.output
