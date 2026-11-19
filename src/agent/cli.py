"""CLI: python -m agent.cli parse "<한국어 명령>" """

from __future__ import annotations

import argparse
import threading
import time
import os
import json
import sys

from .config import get_provider
from .providers.base import ProviderError
from .tasks.parser import parse_command


def _cmd_parse(args: argparse.Namespace) -> int:
    provider = get_provider(args.provider)
    try:
        result = parse_command(provider, args.command)
    except ProviderError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 3

    resp = result.response
    if resp is not None:
        print(
            f"[{resp.provider} {resp.model}] {resp.latency_ms:.0f}ms "
            f"in={resp.usage.input_tokens} out={resp.usage.output_tokens} "
            f"finish={resp.finish_reason}",
            file=sys.stderr,
        )
    if not result.ok:
        print(f"오류: {result.error}", file=sys.stderr)
        if args.raw and resp is not None:
            print(resp.text, file=sys.stderr)
        return 2

    assert result.task is not None and result.raw_task is not None
    if result.task != result.raw_task:
        print("정책 적용: 민감 의도라 needs_approval=true로 변경", file=sys.stderr)
    print(json.dumps(result.task.model_dump(), ensure_ascii=False, indent=2))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    """로컬 합성 사이트에서 에이전트 실행. 민감 작업은 터미널에서 직접 승인한다."""
    from .browser import testenv
    from .browser.client import BrowserSession
    from .core.loop import AgentLoop, Limits
    from .core.verify import LLMVerifier
    from .providers.qwen import QwenProvider
    from .sandbox.manager import SandboxManager
    from .tools import Policy
    from .tools.browser import agent_registry

    sites = {"쇼핑몰": "http://shop.test/", "메일": "http://mail.test/inbox",
             "인트라넷 공지사항": "http://intranet.test/notice", "회의실 예약": "http://booking.test/room"}

    def approver(req) -> bool:
        print("\n⚠️  승인 요청:\n" + req.describe(), file=sys.stderr)
        if req.screenshot_path:
            print(f"  화면: {req.screenshot_path}", file=sys.stderr)
        a = input("실행할까요? [y = 승인 / Enter = 거절 / 고칠 점을 쓰면 고쳐서 다시 시도] ").strip()
        if a.lower() == "y":
            return True
        return a if a and a.lower() != "n" else False

    def user_answer(q: str) -> str | None:
        print(f"\n❓ {q}", file=sys.stderr)
        a = input("답 (빈 줄이면 중단): ").strip()
        return a or None

    testenv.start_site()
    try:
        manager = SandboxManager(network=testenv.NETWORK)
        manager.cleanup_orphans()
        # --user가 있으면 그 사용자의 샌드박스(프로필 유지), 없으면 일회용 브라우저
        ctx = manager.session(args.user) if args.user else BrowserSession(network=testenv.NETWORK)
        shot_dir = None
        if args.screenshots:
            from datetime import datetime
            run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
            shot_dir = str(manager.screenshot_dir(args.user, run_id) if args.user
                           else manager.runs_dir.parent / "screenshots" / run_id)
        if args.user:
            manager.purge_screenshots(args.user)
        with ctx as browser:
            slow = QwenProvider(tool_mode="json", think=True, num_ctx=8192, default_max_tokens=2048)
            loop = AgentLoop(
                QwenProvider(tool_mode="json", think=args.think, num_ctx=8192), browser, agent_registry(),
                Policy(allowed_hosts=frozenset(testenv.SITE_HOSTS), navigate_urls=frozenset(sites.values())),
                sites, approver, limits=Limits(), mode=args.mode, user_answer=user_answer,
                escalate_provider=None if args.think else slow,
                verifier=None if args.no_verify else LLMVerifier(slow, browser),
                log_path=str(manager.user_log_path(args.user)) if args.user else None,
                screenshot_dir=shot_dir,
            )
            context, store = None, None
            if args.user:
                from .memory.qa import memory_context
                from .memory.store import MemoryStore, user_store_path
                if user_store_path(args.user).exists():
                    store = MemoryStore(user_store_path(args.user))
                    context = memory_context(store, args.task)
            plan = None
            if args.plan:
                from .core.plan import format_plan, make_plan
                plan = format_plan(make_plan(slow, args.task, sites, context))
                if plan:
                    print(plan, file=sys.stderr)
            r = loop.run(args.task, context=context, plan=plan)
            if store is not None:
                store.add_task_result(args.task, r.status, r.summary)
        for st in r.steps:
            arg = json.dumps(st.args, ensure_ascii=False) if st.args else ""
            print(f"  {st.step:>2}. {st.tool} {arg[:80]} → {st.result_status or st.check_status}"
                  f" ({st.llm_ms / 1000:.1f}s){' ' + st.note if st.note else ''}", file=sys.stderr)
        print(f"[{r.status}] {r.llm_calls}회 호출, {r.elapsed_s:.1f}초", file=sys.stderr)
        if shot_dir:
            print(f"스크린샷: {shot_dir}", file=sys.stderr)
        print(r.summary)
        if args.show_state:
            print(json.dumps(testenv.site_state(), ensure_ascii=False, indent=2), file=sys.stderr)
    finally:
        testenv.stop_site()
    return 0 if r.status == "completed" else 1


def _cmd_memory(args: argparse.Namespace) -> int:
    """기억: 회의 기록 가져오기(import) / 근거 있는 질문(ask) / 목록(list). 합성 데이터만 넣는다."""
    from .memory.extract import extract_meeting, make_utterances
    from .memory.qa import answer_question
    from .memory.store import MemoryStore, user_store_path
    from .providers.qwen import QwenProvider

    store = MemoryStore(user_store_path(args.user))
    if args.action == "import":
        prov = QwenProvider(think=True, num_ctx=8192, default_max_tokens=3000)
        for r in json.loads(open(args.arg).read()):
            m, st = extract_meeting(prov, r["id"], r["title"], r["date"], make_utterances(r["id"], r["utterances"]))
            store.add_meeting(m)
            print(f"{r['id']} {r['title']}: 항목 {st.items}개, 버림 {st.dropped_no_source}, "
                  f"{st.latency_ms / 1000:.1f}s{' 오류 ' + st.error if st.error else ''}", file=sys.stderr)
        return 0
    if args.action == "import-txt":
        # 실제 회의 데이터(D7): 로컬 모델로만 구조화하고 로컬 저장소에만 둔다. 내용은 출력·로그에 남기지 않는다.
        from .memory.txt_import import TxtFormatError, parse_txt_export
        try:
            parsed = parse_txt_export(open(args.arg, encoding="utf-8").read())
        except (OSError, UnicodeDecodeError, TxtFormatError) as e:
            print(f"가져오기 실패: {e}", file=sys.stderr)
            return 2
        raw = parsed.meeting
        local = QwenProvider(think=True, num_ctx=16384, default_max_tokens=3000)  # 로컬 Ollama 고정
        m, st = extract_meeting(local, raw.id, raw.title, raw.date, raw.utterances)
        if not m.summary and parsed.app_summary:
            m = m.model_copy(update={"summary": parsed.app_summary})
        store.add_meeting(m)
        print(f"가져옴: 발화 {len(m.utterances)}개, 항목 {st.items}개"
              f"{', 구조화 실패(' + st.error + ') — 원문만 저장' if st.error else ''} (id {m.id})", file=sys.stderr)
        return 0
    if args.action == "ask":
        a = answer_question(QwenProvider(num_ctx=8192), store, args.arg)
        print(a.text)
        for u in store.get_utterances([c for c in a.citations if "-u" in c]):
            print(f"  근거 [{u.id}] {u.speaker}: {u.text}", file=sys.stderr)
        return 0 if a.found else 1
    for mid, title, date in store.meetings():
        print(f"{mid}\t{date}\t{title}")
    for created, task, status, summary in store.task_results(5):
        print(f"작업 {created} [{status}] {task[:40]} → {summary[:60]}")
    return 0


def _make_mailer():
    """본인 주소 발송(4단계). 보낼 수 있는 주소 = 앱에서 로그인을 확인하고 연결한 주소(D51) + .env의 SEND_SELF_ADDRESSES(선택).

    @mail.test 주소는 이 프로세스 안의 합성 메일 서버(루프백)로 간다 — 외부로 나가는 메일이 없다.
    Gmail·네이버 주소는 실제 서버(SMTP 465/587, IMAP 993)로 간다. 비밀번호는 키체인에만 있다.
    """
    from pathlib import Path
    from .mail import Mailer
    from .mail.secrets import KeychainSecrets
    from .mail.synthetic import SyntheticBackend, SyntheticMailServer
    from .mail.transport import PROVIDERS, SmtpImapBackend

    addresses = [a for a in os.environ.get("SEND_SELF_ADDRESSES", "").split(",") if a.strip()]
    backends = {name: SmtpImapBackend(hosts) for name, hosts in PROVIDERS.items()}
    # 합성 메일 서버(@mail.test, 외부 발송 없음): 설정에 가상 주소가 있거나 SEND_SELF_SYNTHETIC=1일 때만 켠다(앱 연동 시험용)
    if os.environ.get("SEND_SELF_SYNTHETIC") == "1" or any(a.strip().lower().endswith("@mail.test") for a in addresses):
        backends["test"] = SyntheticBackend(SyntheticMailServer().start())
    base = Path.home() / ".local" / "share" / "local-ai-agent"
    # 다른 사람에게 보내기(D77, 사용자 결정 2026-11-16): 받는 사람은 연락처·사용자가 직접 친 주소만, 보낼 때마다 승인 카드
    return Mailer(addresses, KeychainSecrets(), backends, base / "outbox", state_path=base / "send_log.json",
                  registry_path=base / "mail_accounts.json", allow_others=os.environ.get("SEND_OTHERS", "0") == "1")


def _make_sites(user: str | None = None, mailer=None):
    """실제 사이트 로그인 관리(D52). 로그인용 브라우저는 인터넷에 나가는 별도 컨테이너·별도 프로필을 쓴다."""
    from pathlib import Path
    from .sandbox.manager import SandboxManager
    from .sites import SiteLoginManager

    from .sites.store import KeychainBlobs

    # 일회용 프로필: 브라우저를 닫으면 컨테이너에 아무것도 남지 않는다. 로그인 상태는 키체인에만 둔다(D27의 조건)
    # 실제 사이트는 화면이 크다(네이버 받은메일함은 기본 200줄을 넘는다) → 줄 수 한도를 늘린다
    sandbox = SandboxManager(network="bridge", max_concurrent=1, persist_profile=False,
                             env={"OBS_MAX_LINES": os.environ.get("SITE_OBS_MAX_LINES", "450")})
    base = Path.home() / ".local" / "share" / "local-ai-agent"
    # 공통 로그인(D70)으로 다룰 사이트: 사용자가 정한 것만 맥의 파일에 적는다(저장소 밖). 없으면 지금처럼 네이버뿐이다
    from .sites.login import NAVER, NAVER_MAIL, load_generic_sites
    sites = {"naver": NAVER, "naver_mail": NAVER_MAIL, **load_generic_sites(base / "sites_generic.json")}
    # 전용 크롬 길(꺼 둠): SITE_REAL_CHROME=1이고 맥에서 브라우저 서비스를 돌릴 파이썬(HOST_BROWSER_PYTHON)이 있을 때만 유튜브를 더한다.
    # 구글 계정 로그인은 맥 화면 앞에서 사람이 전용 프로필에 해 둔다(`agent.cli site login youtube`). 격리 컨테이너 밖에서 돈다 — 알고 켠다
    if os.environ.get("SITE_REAL_CHROME", "0") == "1":
        from .browser import hostchrome
        from .sites.login import COUPANG, YOUTUBE
        if hostchrome.available():
            sites["youtube"] = YOUTUBE
            sites["coupang"] = COUPANG   # 가짜 키체인을 뺀 뒤 다시 재니 막지 않았다(D92 이어서) — 사람이 '자동 로그인'으로 해 두면 로그인이 이어진다
        else:
            print("SITE_REAL_CHROME=1이지만 HOST_BROWSER_PYTHON(Playwright가 깔린 파이썬)이나 크롬이 없어 전용 크롬 길을 켜지 않는다", file=sys.stderr)
    mgr = SiteLoginManager(sandbox, KeychainBlobs(), sites, user_id=user or os.environ.get("AGENT_USER_ID", "app"),
                           state_path=base / "sites.json", mailer=mailer,
                           headed=os.environ.get("PUBLIC_BROWSER_HEADED", "0") == "1",
                           sites_path=base / "sites_generic.json")
    from .sites.icons import IconStore
    mgr.icons = IconStore()   # 사이트 아이콘(D110): 로그인 상태를 보관할 때 그 화면에서 받아 둔다 → GET /v1/sites/{site}/icon
    # 풀린 로그인을, 사용자가 처음 고른 소셜 로그인으로 스스로 다시(D90, 사용자 결정 2026-11-16). 아이디·비밀번호는 쓰지 않는다
    mgr.auto_relogin = os.environ.get("SITE_AUTO_RELOGIN", "0") == "1"
    return mgr


def _cmd_site(args: argparse.Namespace) -> int:
    """맥에서 직접 사이트 로그인·확인·로그아웃. 로그인은 사람이 한다(QR을 폰으로 찍거나 여기 입력). 입력한 값은 저장하지 않는다."""
    import base64
    import getpass
    import subprocess
    import tempfile
    import time
    from pathlib import Path
    from .sites import LoginError
    from .providers.claude import load_env_file

    load_env_file()   # 서버와 같은 설정(.env)으로 본다 — 전용 크롬 길의 사이트(SITE_REAL_CHROME)가 이 명령에서도 보이게(2026-11-16: "unknown_site"가 났다)
    m = _make_sites()
    try:
        if args.action == "status":
            print(json.dumps(m.status(), ensure_ascii=False, indent=1))
            return 0
        if args.action == "check":
            print(json.dumps(m.check(args.site), ensure_ascii=False, indent=1))
            return 0
        if args.action == "logout":
            print(json.dumps(m.logout(args.site), ensure_ascii=False, indent=1))
            return 0
        site = m.sites.get(args.site)
        if site is not None and getattr(site, "backend", "sandbox") == "chrome":
            # 전용 크롬 길: 보통의 크롬 창을 전용 프로필로 띄울 뿐이다(자동화 없음). 사람이 로그인하고 창을 닫으면, 에이전트가 그 프로필을 열어 확인한다
            from .browser import hostchrome
            if not os.environ.get("DISPLAY") and os.environ.get("SSH_CONNECTION"):
                print("이 명령은 맥 화면 앞에서 돌려야 해요(원격 접속에서는 창이 보이지 않고, 크롬이 키체인을 쓰지 못해 로그인이 남지 않아요).", file=sys.stderr)
                return 2
            print(f"{site.name} — 전용 프로필로 크롬 창을 엽니다. 직접 로그인한 뒤 그 창을 닫아 주세요. (이 명령은 아무것도 입력하지 않습니다)", flush=True)
            # 구글 계정으로 들어가는 사이트는 구글 로그인 화면을, 그 밖의 사이트는 그 사이트의 로그인 화면을 연다
            url = "https://accounts.google.com/" if site.key == "youtube" else site.login_urls["password"]
            subprocess.run(hostchrome.open_for_login(m.chrome_profile, url), check=False)
            print("확인하는 중…", flush=True)
            print(json.dumps([x for x in m.check(args.site) if x["site"] == args.site], ensure_ascii=False, indent=1))
            return 0
        l = m.start(args.site, args.method)
        shown = -1
        tmp = Path(tempfile.mkdtemp(prefix="laa-login-"))
        while l["state"] not in ("logged_in", "failed", "expired", "cancelled"):
            if l["step"] != shown and l["state"] in ("need_approval", "need_input", "need_choice"):
                shown = l["step"]
                print(f"[{l['state']}] {l['message']} (남은 시간 {l['expires_in_s']}초)", flush=True)
                if l["state"] == "need_approval" and l.get("qr"):
                    path = tmp / f"qr-{shown}.png"
                    path.write_bytes(base64.b64decode(l["qr"]["b64"]))
                    os.chmod(path, 0o600)
                    print(f"QR 그림: {path}" + (f" / 고를 숫자: {l['number']}" if l.get("number") else ""), flush=True)
                    if not args.no_open:
                        subprocess.run(["open", str(path)], check=False)
                elif l["state"] == "need_input":
                    if l.get("image"):
                        path = tmp / f"captcha-{shown}.png"
                        path.write_bytes(base64.b64decode(l["image"]["b64"]))
                        subprocess.run(["open", str(path)], check=False)
                    values = {f["name"]: (getpass.getpass(f"{f['label']}: ") if f["secret"] else input(f"{f['label']}: "))
                              for f in l["fields"]}
                    l = m.input(args.site, l["login_id"], l["step"], values)
                    continue
                elif l["state"] == "need_choice":
                    print(l["text"])
                    for i, c in enumerate(l["choices"], 1):
                        print(f"  {i}. {c}")
                    pick = l["choices"][int(input("번호: ")) - 1]
                    l = m.input(args.site, l["login_id"], l["step"], {"choice": pick})
                    continue
            time.sleep(1.0)
            l = m.get(args.site, l["login_id"])
        print(f"[{l['state']}] {l['message']}" + (f" ({l['reason']})" if l.get("reason") else ""))
        for f in tmp.glob("*.png"):
            f.unlink()
        return 0 if l["state"] == "logged_in" else 1
    except LoginError as e:
        print(f"오류: {e.code}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        m.sandbox.shutdown()


def _cmd_serve(args: argparse.Namespace) -> int:
    """앱용 HTTP API 서버 (127.0.0.1 전용). 명세: docs/API.md"""
    from pathlib import Path
    from .api.jobs import JobManager
    from .api.runner import AgentRunner
    from .api.server import ensure_token, make_server

    env_path = Path(__file__).resolve().parents[2] / ".env"
    token = ensure_token(env_path)
    mailer = _make_mailer()
    sites = _make_sites(mailer=mailer)
    runner = AgentRunner(mailer=mailer, sites=sites)
    find_on = os.environ.get("DIALOG_WEB_FIND", "0") == "1"
    browse_on = os.environ.get("DIALOG_WEB_BROWSE", "0") == "1"
    runner.public_headed = os.environ.get("PUBLIC_BROWSER_HEADED", "0") == "1"   # 공개 길만. 로그인하는 사이트 흐름은 그대로
    # 로그인한 화면을 Claude가 보는 것을 모든 사이트에 미리 허락(D75, 사용자 결정 2026-11-16 — 혼자 쓰는 동안, 운전 모델을 로컬로 바꾸기 전까지). 끄면 사이트마다 카드로 묻는다
    runner.cloud_all = os.environ.get("SITE_CLOUD_ALL", "0") == "1"
    # 자동 도구를 쓰지 말라고 안내했던 사이트는 적어 두고 공개 길에서 다시 열지 않는다(호스트뿐, 저장소 밖)
    from pathlib import Path as _Path
    from .sites.public import use_refused
    runner.refused_path = _Path.home() / ".local" / "share" / "local-ai-agent" / "sites_refused.json"
    use_refused(runner.refused_path)
    runner.start()
    manager = JobManager(runner)
    from .api.dialog import DialogManager
    from .providers.qwen import QwenProvider
    # 대화 담당(D43): 작업 담당과 같은 로컬 모델. AGENT_DIALOG_MODE = hybrid(기본: think 끔으로 고르고 task·'발췌 있는
    # none'만 think 켬으로 재판단) | off | on
    mode = os.environ.get("AGENT_DIALOG_MODE", "hybrid")
    mk = lambda think: QwenProvider(model=runner.model, tool_mode="json", think=think, num_ctx=8192, timeout=120.0,
                                    default_max_tokens=3000 if think else 800)
    # 대화 판단을 Claude가(D59): DIALOG_HEAD=claude + DIALOG_MODEL=<모델 id>. 키(.env의 ANTHROPIC_API_KEY)나 모델이 없으면 로컬 그대로.
    # 부르지 못한 말은 그 한 번만 로컬 판단으로 처리한다. DIALOG_SEND_CONTEXT=0이면 회의 자료의 내용은 보내지 않는다(붙어 있다는 사실만)
    # 공개 정보 웹 검색(D60): DIALOG_WEB_SEARCH=1이고 Claude 판단이 켜져 있을 때만. DIALOG_WEB_MODEL로 답을 쓰는 모델을 따로 고를 수 있다
    cloud = web = None
    if os.environ.get("DIALOG_HEAD", "local") == "claude":
        from .providers.claude import ClaudeProvider, has_api_key
        cmodel = os.environ.get("DIALOG_MODEL", "")
        if cmodel and has_api_key():
            cloud = ClaudeProvider(model=cmodel, think=False, effort=os.environ.get("DIALOG_EFFORT", "low"), timeout=20.0,
                                   cache_system=True, max_retries=1)
            if os.environ.get("DIALOG_WEB_SEARCH", "0") == "1":
                web = ClaudeProvider(model=os.environ.get("DIALOG_WEB_MODEL") or cmodel, think=False, effort="low", timeout=40.0,
                                     max_retries=1)
        else:
            print("DIALOG_HEAD=claude지만 DIALOG_MODEL 또는 API 키가 없어 로컬 판단으로 띄운다", file=sys.stderr)
    dialog = DialogManager(mk(mode == "on"), manager, escalate=mk(True) if mode == "hybrid" else None, mailer=mailer,
                           sites=sites, cloud=cloud, send_context=os.environ.get("DIALOG_SEND_CONTEXT", "1") != "0", web=web,
                           web_find=find_on and web is not None and sites is not None,
                           web_browse=browse_on and web is not None and sites is not None)
    # 로그인한 사이트 안에서 하기(D72): 화면은 Claude(운전 모델)가 본다 — 사이트마다 사용자의 허락을 작업 안에서 묻는다. 대화 판단이 Claude일 때만
    # 사진·PDF 붙여 묻기(D73, 사용자 결정 2026-11-15): 붙인 것은 맥 메모리에만 두고, 그 대화의 답을 위해 Claude로 간다. 대화 판단이 Claude일 때만
    if os.environ.get("DIALOG_UPLOADS", "0") == "1" and cloud is not None:
        from .api.uploads import UploadStore
        dialog.uploads = UploadStore()
    dialog.site_do = os.environ.get("DIALOG_SITE_DO", "0") == "1" and cloud is not None and sites is not None
    # 실제로 쓰는 서버: 개발용 테스트 사이트의 작업을 사용자에게 만들지 않는다(DIALOG_TEST_SITES=1이면 예전처럼 만든다 — 개발·측정용)
    dialog.real_only = cloud is not None and os.environ.get("DIALOG_TEST_SITES", "0") != "1"
    if dialog.site_do and not runner.browse_model:
        runner.browse_model = os.environ.get("DIALOG_BROWSE_MODEL", "claude-haiku-5-5")
    dialog.shop_view = os.environ.get("DIALOG_SHOP_VIEW", "1") != "0"   # 0이면 폰의 앱 안 쇼핑 화면을 여는 1단계(action shop)도 끈다
    shop_keys = (os.environ.get("NAVER_SEARCH_ID", "").strip(), os.environ.get("NAVER_SEARCH_SECRET", "").strip())
    shop_api_wanted = os.environ.get("DIALOG_SHOP_API", "0") == "1" and cloud is not None
    if shop_api_wanted and all(shop_keys):   # 쇼핑(D71): 공식 쇼핑 검색 API로 찾아 카드로. 키가 없으면 켜지 않는다(살 것을 찾는 말은 웹 검색 길로 간다)
        dialog.shop_api = True
        runner.shop_api_keys = shop_keys
        runner.shop_model = os.environ.get("DIALOG_SHOP_MODEL", "claude-haiku-5-5")
    if os.environ.get("DIALOG_SHOP_DRIVE", "0") == "1" and cloud is not None:   # 쇼핑(D68): 폰의 쇼핑 화면을 멀리 있는 브라우저로
        dialog.shop_drive = True
        runner.shop_model = os.environ.get("DIALOG_SHOP_MODEL", "claude-haiku-5-5")
    if dialog.web_browse:   # 일반 길(D67): 공개 사이트를 스스로 조작한다. 운전은 DIALOG_BROWSE_MODEL(기본 haiku — 측정에서 쉬운 과제 10/10, 과제당 몇 원)
        runner.browse_model = os.environ.get("DIALOG_BROWSE_MODEL", "claude-haiku-5-5")
        runner.browse_web = web
    if dialog.web_find:   # 공개 정보 직접 찾기(D61): 후보 페이지는 네이버 검색 API(키가 있으면) 또는 Claude의 웹 검색으로 찾는다
        from .sites.public import ClaudeFinder, NaverFinder
        nid, nsec = os.environ.get("NAVER_SEARCH_ID", ""), os.environ.get("NAVER_SEARCH_SECRET", "")
        # 키가 있다고 저절로 바꾸지 않는다(쇼핑 검색에 쓰려고 넣은 키가 찾기의 동작까지 바꾸지 않게) — DIALOG_WEB_FIND_FINDER=naver일 때만
        use_naver = os.environ.get("DIALOG_WEB_FIND_FINDER", "") == "naver" and nid and nsec
        runner.finder = NaverFinder(nid, nsec) if use_naver else ClaudeFinder(web)
        runner.find_claude = web   # DIALOG_WEB_FIND_READER=claude일 때만 쓰인다(연 페이지의 글이 Claude로 간다)
    print(f"대화 판단: {'Claude ' + cloud.model if cloud is not None else '로컬 ' + runner.model}"
          f"{', 공개 정보 웹 검색 켬(' + web.model + ')' if web is not None else ''}"
          f"{', 직접 찾기 켬(후보 찾기: ' + runner.finder.name + ', 답: ' + runner.find_reader + ')' if dialog.web_find else ''}"
          f"{', 일반 길 켬(운전: ' + runner.browse_model + ')' if dialog.web_browse else ''}"
          f"{', 쇼핑(폰 화면) 켬' if dialog.shop_drive else ''}"
          f"{', 사이트 안에서 하기 켬' if dialog.site_do else ''}"
          f"{', 사진·PDF 붙이기 켬' if dialog.uploads is not None else ''}"
          f"{'' if dialog.shop_view else ', 쇼핑 화면 열기 끔'}"
          f"{', 쇼핑 검색 API 켬' if dialog.shop_api else ', 쇼핑 검색 API: 키 없음(꺼짐)' if shop_api_wanted else ''}"
          f"{', 공개 길 브라우저: 창 모드' if runner.public_headed else ''}"
          f"{', 회의 자료 내용은 보내지 않음' if cloud is not None and not dialog.send_context else ''}", file=sys.stderr)
    # 푸시 알림(D44): 키체인의 APNs 키와 .env의 APNS_KEY_ID·APNS_TEAM_ID·APNS_TOPIC이 다 있을 때만 켜진다
    from .api.push import make_notifier
    push = make_notifier(manager.approval_timeout_s)
    if push is not None:
        manager.on_status = push.on_status
    print(f"푸시 알림: {'켬(기기 ' + str(len(push.store.tokens())) + '대 등록)' if push is not None else '끔(APNs 키 설정 없음)'}", file=sys.stderr)
    # 로그인 살려 두기(D105): 몇 시간마다 로그인해 둔 사이트를 한 번 열어 세션을 잇고, 풀린 것을 일보다 먼저 알린다(SITE_KEEPALIVE_H, 0이면 끔)
    keep_h = float(os.environ.get("SITE_KEEPALIVE_H", "6") or 0)
    if sites is not None and keep_h > 0:
        from .sites.keepalive import KeepAlive
        KeepAlive(sites, manager, push, every_h=keep_h).start()
    print(f"로그인 살려 두기: {f'{keep_h:g}시간마다' if sites is not None and keep_h > 0 else '끔'}", file=sys.stderr)
    # 받은 메일을 Claude가 읽는다(D82, MAIL_CLOUD=1, 사용자 결정 2026-11-16): 브리핑과 이어지는 물음을 Claude가 쓴다. 대화 판단이 Claude일 때만
    dialog.mail_cloud = os.environ.get("MAIL_CLOUD", "0") == "1" and dialog.cloud is not None and mailer is not None
    print(f"메일 읽기: {'Claude가 읽음(보낸 사람·제목·본문 앞부분이 나간다)' if dialog.mail_cloud else '맥의 로컬 모델만'}", file=sys.stderr)
    # 예약해 둔 일(D80, ROUTINES=1): 사용자가 정한 때에 사용자가 정한 말을 대신 건넨다. 바꾸는 일은 그때도 승인 카드에서 멈춘다
    if os.environ.get("ROUTINES", "0") == "1" and dialog.cloud is not None:
        from pathlib import Path as _P
        from .api.routines import RoutineStore, Scheduler
        dialog.routines = RoutineStore(_P.home() / ".local" / "share" / "local-ai-agent" / "routines.json")

        def _space_then_notify(rec: dict, kind: str) -> None:
            """내 공간의 카드에 이은 예약이 돌았다(D87): 카드를 미리 채우고, **내용이 바뀌었을 때만** 알린다(처음 채운 것도 알린다)."""
            def go():
                first = not any(x.get("routine_id") == rec["routine_id"] and x["id"] != rec["id"] and x.get("status") in ("done", "task")
                                for x in dialog.scheduler.recent(50, rec["routine_id"]))
                try:
                    changed = dialog.space.on_routine_done(rec["routine_id"])
                except Exception as e:
                    sys.stderr.write(f"[space] {time.strftime('%Y-%m-%d %H:%M:%S')} on_routine_done error={type(e).__name__}\n")
                    changed = ["?"]
                sys.stderr.write(f"[space] {time.strftime('%Y-%m-%d %H:%M:%S')} routine done changed={len(changed)} first={'y' if first else 'n'}\n")
                if push is not None and (changed or first):
                    push.notify(kind, rec["id"], rec.get("conversation_id"))
            threading.Thread(target=go, name="space-fill", daemon=True).start()

        def _routine_notice(rec: dict) -> None:
            if rec["status"] == "done" and getattr(dialog, "space", None) is not None and dialog.space.linked(rec["routine_id"]):
                return _space_then_notify(rec, "routine_result")
            if push is not None:
                kind = {"needs_login": "needs_login", "needs_reply": "input_needed", "failed": "task_failed", "missed": "task_failed"}.get(rec["status"], "routine_result")
                push.notify(kind, rec["id"], rec.get("conversation_id"))
        # 실행 기록은 파일에 남긴다(그때의 답 포함 — 사용자 결정 2026-11-16). 예약이 만든 작업이 끝나면 그 결과도 기록에 붙인다
        dialog.scheduler = Scheduler(dialog.routines, dialog.run_routine, _routine_notice,
                                     runs_path=_P.home() / ".local" / "share" / "local-ai-agent" / "routine_runs.json").start()
        _prev_status = manager.on_status

        def _status(job, status):
            rec = next((x for x in dialog.scheduler.recent(50) if x.get("task_id") == job.id), None) if status == "completed" else None
            if rec is None and status == "completed":   # 예약이 멈췄던 일을 같은 대화에서 이어 한 작업인가(다시 로그인한 뒤 등)
                rep_ = ((job.result or {}).get("report") or {})
                rec = dialog.scheduler.task_followup(job.conversation_id, job.id, status,
                                                     list(rep_.get("done") or []) or [str((job.result or {}).get("summary") or "")])
            tied = rec is not None and getattr(dialog, "space", None) is not None and dialog.space.linked(rec["routine_id"])
            if _prev_status is not None and not tied:   # 카드에 이은 예약의 작업이 끝난 알림은 아래에서, 내용이 바뀌었을 때만 보낸다
                _prev_status(job, status)
            if status in ("completed", "failed", "needs_input"):
                rep = ((job.result or {}).get("report") or {})
                lines = list(rep.get("done") or []) + [f"(못 한 것) {x}" for x in (rep.get("blocked") or [])]
                dialog.scheduler.task_finished(job.id, status, lines or [str((job.result or {}).get("summary") or "")])
            if tied:
                _space_then_notify(rec, "routine_result")
        manager.on_status = _status
    # 작업실(D96, WORKROOM=1): 파일로 된 결과물을 만드는 부탁은 격리된 컨테이너에서 Claude가 코드를 짜서 만든다
    dialog.workroom = os.environ.get("WORKROOM") == "1" and dialog.cloud is not None
    # 답변 카드(사용자 지시 2026-11-16): 끝난 작업의 결과를 정해진 꼴의 카드로. 맥에서만 읽은 결과(정해진 사이트 읽기)는 로컬 모델이 다듬는다
    from .api.cards import make_card
    from .sites.read import PAGES as _PAGES
    from .api.activity import ActivityLog
    manager.activity = ActivityLog()   # 활동 탭·검색(D103)
    from .api.failures import FailureLog
    manager.failures = FailureLog()    # 실사용 실패의 꼴(내용 없음) — scripts/failures_report.py
    manager.card_maker = lambda job, result: make_card(job, result, dialog.cloud, dialog.provider, private=job.skill in _PAGES)
    # 통화 목소리(D117, VOICE_TTS=1): 사용자의 목소리 샘플로 맥이 음성을 만든다(전용 가상환경의 일꾼, 처음 부를 때 띄움)
    from .voice.engine import VoiceEngine
    dialog.voice = VoiceEngine.from_env()
    # 오래 가는 기억(D129, MEMORY=1, 사용자 결정 2026-11-18): 요약본 profile.md(매 말마다 Claude 판단에 붙는다) + 옛 대화 보관함(찾을 때만).
    # 요약본은 대화가 잠잠해지면 MEMORY_MODEL(없으면 DIALOG_WEB_MODEL·DIALOG_MODEL)이 한 번에 정리한다. 대화 판단이 Claude일 때만
    from .sites import inside as _inside_mod   # 사람이 풀어도 계속 막은 사이트(D128): 다음엔 곧바로 '폰에서 열기'
    _inside_mod.use_walls(__import__("pathlib").Path.home() / ".local" / "share" / "local-ai-agent" / "sites_walls.json")
    dialog.memory = None
    if os.environ.get("MEMORY", "0") == "1" and dialog.cloud is not None:
        from pathlib import Path as _PM
        from .memory.user import Digester, UserMemory
        from .providers.claude import ClaudeProvider as _CPM
        dialog.memory = UserMemory(_PM.home() / ".local" / "share" / "local-ai-agent" / "memory")
        mmodel = os.environ.get("MEMORY_MODEL") or os.environ.get("DIALOG_WEB_MODEL") or os.environ.get("DIALOG_MODEL", "")
        dialog.memory_digester = Digester(_CPM(model=mmodel, think=False, effort="low", timeout=60.0, max_retries=1), dialog.memory).start()
    # 내 공간(D87, SPACE=1): 말로 짜는 카드 화면. 구성은 Claude가 내고 코드가 검사한다. 대화 판단이 Claude일 때만
    dialog.space = None
    if os.environ.get("SPACE", "0") == "1" and dialog.cloud is not None:
        from pathlib import Path as _P2
        from .api.space import Space
        dialog.space = Space(_P2.home() / ".local" / "share" / "local-ai-agent" / "space.json", dialog.cloud, routines=dialog.routines,
                             data_path=_P2.home() / ".local" / "share" / "local-ai-agent" / "space_data.json",
                             scheduler=getattr(dialog, "scheduler", None), mailer=mailer, local=dialog.provider, ask=dialog.ask_for_space)
        dialog.space.mail_cloud = dialog.mail_cloud
        # 추천 카드에 넣을 '로그인해 둔 사이트에서 읽을 수 있는 것': 끝까지 확인된 것만 적는다(지금은 네이버페이 결제 내역 하나 — 2026-11-16 실제로 1/1)
        try:
            naver_in = sites is not None and any(x["site"] == "naver" and x["status"] == "logged_in" for x in sites.status())
        except Exception:
            naver_in = False
        dialog.space.site_cards = [("네이버페이 결제 내역", "네이버페이 최근 결제 내역 카드 넣어줘")] if naver_in else []
    print(f"내 공간: {'켬(카드 ' + str(len(dialog.space.cards)) + '장)' if dialog.space is not None else '끔'}", file=sys.stderr)
    print(f"예약(루틴): {'켬(' + str(len(dialog.routines.list())) + '개)' if dialog.routines is not None else '끔'}", file=sys.stderr)
    srv = make_server(manager, token, args.port, dialog, mailer, sites, push=push)
    print(f"API 서버: http://127.0.0.1:{args.port} (루프백 전용, 토큰은 .env의 AGENT_APP_TOKEN), 모델 {runner.model}",
          file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        manager.shutdown()
        runner.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent")
    parser.add_argument("--provider", default=None, help="MODEL_PROVIDER 덮어쓰기")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("parse", help="한국어 명령을 TaskRequest JSON으로 변환")
    p.add_argument("command")
    p.add_argument("--raw", action="store_true", help="실패 시 모델 원문 출력")
    p.set_defaults(func=_cmd_parse)
    r = sub.add_parser("run", help="로컬 합성 사이트에서 에이전트 실행 (colima·Ollama 필요)")
    r.add_argument("task")
    r.add_argument("--think", action="store_true", help="모든 단계 thinking 켜기 (기본: 끔 + 막힐 때만 켬)")
    r.add_argument("--mode", choices=["act", "observe"], default="act", help="observe: 상태 변경 동작 실행 안 함")
    r.add_argument("--show-state", action="store_true", help="끝난 뒤 사이트 상태 출력")
    r.add_argument("--user", default=None, help="사용자 id: 사용자별 샌드박스·프로필·로그 사용 (합성 id만)")
    r.add_argument("--screenshots", action="store_true", help="단계별·승인 요청·완료 화면을 이미지로 저장 (사람 확인용)")
    r.add_argument("--plan", action="store_true", help="실행 전에 계획을 세운다 (조건이 있는 여러 단계 작업에 권장)")
    r.add_argument("--no-verify", action="store_true", help="완료 검증 끄기 (기본: 완료 선언을 화면으로 검증)")
    r.set_defaults(func=_cmd_run)
    sv = sub.add_parser("serve", help="앱용 HTTP API 서버 (127.0.0.1 전용, 읽기·초안만)")
    sv.add_argument("--port", type=int, default=8765)
    sv.set_defaults(func=_cmd_serve)
    st = sub.add_parser("site", help="실제 사이트 로그인(사람이 직접): login | status | check | logout (D52)")
    st.add_argument("action", choices=["login", "status", "check", "logout"])
    st.add_argument("site", nargs="?", default="naver")
    st.add_argument("--method", choices=["qr", "password"], default="qr")
    st.add_argument("--no-open", action="store_true", help="QR 그림을 화면에 띄우지 않는다(경로만 출력)")
    st.set_defaults(func=_cmd_site)
    m = sub.add_parser("memory", help="기억: import <합성 회의.json> | import-txt <앱 TXT 내보내기> | ask <질문> | list")
    m.add_argument("action", choices=["import", "import-txt", "ask", "list"])
    m.add_argument("arg", nargs="?", default="")
    m.add_argument("--user", required=True, help="사용자 id")
    m.set_defaults(func=_cmd_memory)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
