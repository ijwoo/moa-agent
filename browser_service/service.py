"""브라우저 서비스 (컨테이너 안에서 실행). 표준 입력으로 JSON 명령을 한 줄씩 받고, 표준 출력으로 한 줄씩 답한다.

요청:  {"id": 1, "op": "navigate", "args": {"url": "http://shop.test/"}}
응답:  {"id": 1, "ok": true, "result": {...}}  /  {"id": 1, "ok": false, "error": "..."}

허용 op: navigate, observe, click, type, select_option, scroll, extract_text, field_values, screenshot, close — 이 밖의 요청은 거절한다.
공개 페이지 읽기 전용 op(호스트의 코드만 부른다. 모델의 도구 목록에는 없다): links(화면의 링크 글·주소), meta(제목·대표 그림 주소).
로그인 전용 op(호스트의 로그인 코드만 부른다. 모델의 도구 목록에는 없다): secret_type, element_screenshot, cookie_names,
cookies_export, cookies_import, field_secret, dialog_policy.
주 검증은 호스트(validate_call)가 하고, 여기서는 최소한의 방어(스킴·길이·ref 형식)를 한 번 더 한다.
표준 출력은 프로토콜 전용이다. 로그는 표준 오류로만.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

sys.path.insert(0, "/app")
from snapshot import format_observation_with_refs  # noqa: E402

MAX_REQUEST_BYTES = 64 * 1024
ACTION_TIMEOUT_MS = 10_000
NAV_TIMEOUT_MS = 15_000
MAX_EXTRACT_CHARS = int(os.environ.get("EXTRACT_MAX_CHARS", "8000"))   # 모델에게 주는 화면 글 한도(D121: 4000 → 8000, 빈칸은 줄인 뒤)
REF = re.compile(r"^e\d+$")
MAX_FIELDS = 100
STORAGE_VALUE_MAX = 100_000   # 브라우저 저장소의 값 하나의 크기 한도(로그인 표식은 작다. 큰 값은 캐시라 보관하지 않는다)
STORAGE_ITEMS_MAX = 60
RESTORED_MARK = "__laa_restored"   # 이 탭에 보관한 저장소 값을 이미 넣었다는 표식
# 내려받은 표(D125): 공개 길의 세션에서만 켠다(ALLOW_DOWNLOADS=1). 파일은 /tmp(크기 제한 tmpfs)에 잠깐 두고 읽은 뒤 지운다
DOWNLOADS = os.environ.get("ALLOW_DOWNLOADS") == "1"
# 로그인한 사이트의 세션(ALLOW_DOWNLOADS=gated): 브라우저는 받을 수 있게 띄우되, 사용자가 그 사이트에 허락한 뒤에만 표로 받는다(D126)
PUBLIC_DOWNLOADS = os.environ.get("DOWNLOADS_GATE") != "1"
THIN_CHARS = 60      # 이보다 글이 적으면 아직 그리는 중으로 보고 잠깐 기다린다(observe)
MAX_FIELD_CHARS = 20_000  # type 입력 상한(5000자)보다 넉넉히. 이를 넘으면 호스트가 길이 불일치로 본다
# select는 선택된 항목 이름, 비밀번호는 가림, 입력칸·textarea는 value, 그 밖(contenteditable)은 innerText
FIELD_VALUE_JS = """e => {
  if (e.tagName === 'SELECT') return Array.from(e.selectedOptions).map(o => o.label).join(', ');
  if (e.type === 'password') return e.value ? '\u2022\u2022\u2022\u2022' : '';
  if ('value' in e && typeof e.value === 'string') return e.value;
  return e.innerText;
}"""


# 승인 화면용: 이름표 없는 입력 칸을 사람이 알아볼 이름. 접근성 이름(aria-label·label·placeholder·title)이 먼저, 없으면 바로 앞의 짧은 글, 없으면 칸의 종류.
# 실기기: 이름표 없는 날짜 칸이 승인 카드에 "2026-11-16 2026-11-16"(이름 자리에 값), 이름표 없는 빈 칸이 이름 없이 "(비어 있음)"으로 나왔다
FIELD_NAME_JS = """e => {
  const t = s => (s || '').replace(/\\s+/g, ' ').trim();
  const by = (e.getAttribute('aria-labelledby') || '').split(/\\s+/).map(i => i && document.getElementById(i)).filter(Boolean).map(x => t(x.innerText)).join(' ');
  const own = t(e.getAttribute('aria-label')) || t(by) || (e.labels && e.labels[0] ? t(e.labels[0].innerText) : '') || t(e.getAttribute('placeholder')) || t(e.getAttribute('title'));
  if (own) return own.slice(0, 30);
  const FIELD = 'input, textarea, select, [contenteditable=""], [contenteditable=true], button';
  let cur = e;
  for (let depth = 0; depth < 3 && cur && cur !== document.body; depth++) {
    const p = cur.previousElementSibling;
    if (p) {
      const s = t(p.innerText);
      if (s && s.length <= 30 && !p.matches(FIELD) && !p.querySelector(FIELD)) return s;
      break;
    }
    cur = cur.parentElement;
  }
  const KIND = {date: '날짜', 'datetime-local': '날짜와 시간', time: '시간', month: '월', week: '주', number: '숫자', email: '이메일', tel: '전화번호', url: '주소'};
  return KIND[(e.getAttribute('type') || '').toLowerCase()] || '';
}"""


# 화면 위에 떠 있는 안내 창의 글(6개, 300자까지). 사이트가 "자동 도구를 쓰지 말라"고 띄운 창을 호스트가 알아보게 한다 —
# 실기기: 그런 창이 떴는데 관찰 결과의 긴 글에 묻혀 못 알아보고 계속 눌렀다. 큰 영역(600자 초과)은 안내 창이 아니라 본문이다
NOTICE_JS = """() => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 40 && r.height > 20 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0'; };
  const out = [];
  const SEL = '[role=dialog], [role=alertdialog], [role=alert], [aria-modal="true"], dialog[open], [class*="modal" i], [class*="popup" i], [class*="layer" i], [class*="alert" i], [class*="dialog" i]';
  const take = e => {
    if (out.length >= 6 || !vis(e)) return;
    const t = (e.innerText || '').replace(/\\s+/g, ' ').trim();
    if (t.length >= 8 && t.length <= 600 && !out.some(o => o === t.slice(0, 300) || t.includes(o))) out.push(t.slice(0, 300));
  };
  document.querySelectorAll(SEL).forEach(take);
  // 이름으로는 알 수 없는 창: 화면에 고정돼 떠 있는 것(position fixed, 또는 z-index가 큰 absolute). 요소가 아주 많은 화면은 앞의 5,000개만 본다
  const all = document.body ? document.body.getElementsByTagName('*') : [];
  for (let i = 0; i < all.length && i < 5000 && out.length < 6; i++) {
    const s = getComputedStyle(all[i]);
    if (s.position === 'fixed' || (s.position === 'absolute' && parseInt(s.zIndex || '0', 10) >= 100)) take(all[i]);
  }
  return out;
}"""


class BadRequest(Exception):
    pass


def _str(args: dict, key: str, max_len: int, required: bool = True) -> str | None:
    v = args.get(key)
    if v is None and not required:
        return None
    if not isinstance(v, str) or not v or len(v) > max_len:
        raise BadRequest(f"{key}: 문자열(1~{max_len}자)이어야 한다")
    return v


def _ref(args: dict, required: bool = True) -> str | None:
    r = _str(args, "ref", 16, required)
    if r is not None and not REF.match(r):
        raise BadRequest("ref 형식 오류")
    return r


# 로그인 화면의 칸을 표준 속성으로 알아본다(낱말·사이트 이름에 기대지 않는다, D70). 값은 읽지 않는다.
# 돌려주는 것: 칸의 차례(i: 그 문서의 input 가운데 몇 번째)·역할·종류·이름표, 제출 단추의 차례, 캡차 그림의 차례.
LOGIN_FORM_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none' && !e.disabled && !e.readOnly; };
  const attrs = e => [e.name, e.id, e.getAttribute('placeholder'), e.getAttribute('aria-label'), e.getAttribute('autocomplete'), e.getAttribute('title')].join(' ');
  const kindOf = e => { const t = (e.getAttribute('type') || 'text').toLowerCase();
    if (t === 'password') return 'password';
    if (!['text', 'email', 'tel', 'number', ''].includes(t)) return null;
    const ac = (e.getAttribute('autocomplete') || '').toLowerCase(), im = (e.getAttribute('inputmode') || '').toLowerCase();
    const ml = parseInt(e.getAttribute('maxlength') || '0', 10);
    if (ac === 'one-time-code' || ((im === 'numeric' || t === 'number') && ml >= 4 && ml <= 8)) return 'code';
    return 'text'; };
  const isSearch = e => (e.getAttribute('type') || '').toLowerCase() === 'search' || e.getAttribute('role') === 'searchbox'
    || /search|query|keyword|kwd|검색/i.test(attrs(e));
  const looksId = e => /user|login|account|email|e-mail|\bid\b|userid|memberid|phone|tel|아이디|이메일|휴대폰|전화|계정/i.test(attrs(e))
    || ['email', 'tel'].includes((e.getAttribute('type') || '').toLowerCase());
  const isCaptcha = e => /captcha|보안문자|자동입력|자동 입력|방지문자|문자를 입력/i.test(attrs(e));
  const label = e => { let s = e.getAttribute('aria-label') || '';          // 칸의 이름(화면 읽기 프로그램이 읽는 것)이 먼저, 예시 글(placeholder)은 그다음
    if (!s && e.labels && e.labels.length) s = e.labels[0].innerText || '';
    if (!s) s = e.getAttribute('placeholder') || e.getAttribute('title') || '';
    return s.replace(/\s+/g, ' ').trim().slice(0, 40); };
  const inputs = Array.from(document.querySelectorAll('input'));
  const cand = inputs.map((e, i) => ({e, i, kind: kindOf(e)})).filter(x => x.kind && vis(x.e));
  const pws = cand.filter(x => x.kind === 'password');
  const picked = [];
  let signup = false;
  if (pws.length) {
    const pw = pws[0];
    const near = x => pw.e.form ? x.e.form === pw.e.form : Math.abs(cand.indexOf(x) - cand.indexOf(pw)) <= 3;
    // 가입·비밀번호 바꾸기 화면: 비밀번호 칸 바로 다음 칸이 또 비밀번호다(비밀번호 + 확인). 같은 화면의 다른 묶음(비회원 조회 등)에 있는
    // 비밀번호 칸은 상관없다 — 실측: 회원 로그인과 비회원 예매 확인이 한 화면에 있는 사이트를 가입 화면으로 잘못 봤다
    const nextC = cand[cand.indexOf(pw) + 1];
    signup = !!(nextC && nextC.kind === 'password' && near(nextC));
    const before = cand.filter(x => x.i < pw.i && x.kind === 'text' && !isSearch(x.e) && !isCaptcha(x.e));
    const same = before.filter(near);
    const idf = (same.length ? same : before).slice(-1)[0];
    if (idf) picked.push({...idf, role: 'id'});
    picked.push({...pw, role: 'password'});
    const cap = cand.filter(x => x.i > pw.i && x.kind !== 'password' && near(x) && isCaptcha(x.e))[0];
    if (cap) picked.push({...cap, role: 'captcha', kind: 'captcha'});
  } else {
    const code = cand.filter(x => x.kind === 'code')[0];
    const caps = cand.filter(x => x.kind === 'text' && isCaptcha(x.e));
    const ids = cand.filter(x => x.kind === 'text' && !isSearch(x.e) && !isCaptcha(x.e) && looksId(x.e));
    if (code) picked.push({...code, role: 'code'});
    else if (ids.length === 1) picked.push({...ids[0], role: 'id'});     // 두 쪽짜리 로그인의 첫 쪽(아이디만)
    else if (caps.length === 1) picked.push({...caps[0], role: 'captcha', kind: 'captcha'});
  }
  const BTN = 'button, input[type=submit], input[type=button], [role=button], a';
  const btns = Array.from(document.querySelectorAll(BTN));
  const txt = b => ((b.innerText || b.value || b.getAttribute('aria-label') || '') + '').replace(/\s+/g, ' ').trim();
  const good = b => /^(로그인|로그인하기|login|log in|sign in|다음|계속|계속하기|확인|next|continue|인증|인증하기|제출)$/i.test(txt(b));
  let submit = -1;
  const last = picked[picked.length - 1];
  if (last) {
    const form = last.e.form;
    let b = null;
    if (form) { const c = Array.from(form.querySelectorAll('button[type=submit], input[type=submit], button:not([type])')).filter(vis);
      // 글이 맞는 단추가 먼저. 글 없는 단추(비밀번호 보기·입력 지우기 같은 아이콘)는 제출 단추가 아니다
      b = c.filter(good)[0] || btns.filter(x => vis(x) && form.contains(x) && good(x))[0] || c.filter(x => txt(x).length > 0)[0] || null; }
    if (!b) b = btns.filter(x => vis(x) && (last.e.compareDocumentPosition(x) & Node.DOCUMENT_POSITION_FOLLOWING) && good(x))[0];
    if (b) submit = btns.indexOf(b);
  }
  let image = -1;
  const capf = picked.filter(x => x.role === 'captcha')[0];
  if (capf) {
    const imgs = Array.from(document.querySelectorAll('img, canvas'));
    const scope = capf.e.form || capf.e.parentElement && capf.e.parentElement.parentElement || document;
    const im = imgs.filter(x => vis(x) && /captcha|보안|자동입력|방지/i.test([x.id, x.className, x.getAttribute('alt'), x.getAttribute('src')].join(' ')))[0]
      || imgs.filter(x => vis(x) && scope.contains(x) && x.getBoundingClientRect().width >= 60)[0];
    if (im) image = imgs.indexOf(im);
  }
  // 제출 뒤 무슨 일이 있었는지 가르는 꼴(값·글은 읽지 않는다): 비밀번호 칸이 비워졌나, 오류 안내가 떴나, 화면을 덮는 층이나 확인 절차(캡차)가 있나, 제출 단추가 꺼져 있나
  const shown = e => { const r = e.getBoundingClientRect(), st = getComputedStyle(e); return r.width > 2 && r.height > 2 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const some = (sel, f) => { try { return Array.from(document.querySelectorAll(sel)).some(f); } catch (e) { return false; } };
  const ALERT = '[role=alert], [aria-live=assertive], [class*="error" i], [class*="invalid" i], [class*="warn" i]';
  const isAlert = e => shown(e) && (e.innerText || '').trim().length > 1 && (e.innerText || '').length < 300;
  const alert = some(ALERT, isAlert) || some('input[aria-invalid="true"]', shown);
  let alertText = '';
  try { const a = Array.from(document.querySelectorAll(ALERT)).filter(isAlert)[0]; if (a) alertText = (a.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 140); } catch (e) {}
  const dialog = some('[role=dialog], [role=alertdialog], [aria-modal="true"], dialog[open]', shown);
  const challenge = some('iframe', f => shown(f) && /recaptcha|hcaptcha|turnstile|captcha|challenge/i.test(f.src || ''))
    || some('[class*="captcha" i], [id*="captcha" i]', e => shown(e) && e.tagName !== 'INPUT');
  const sb = submit >= 0 ? btns[submit] : null;
  return {fields: picked.map(x => ({i: x.i, role: x.role, kind: x.kind, label: label(x.e)})), submit, image, signup,
          passwords: pws.length, inForm: !!(last && last.e.form),
          pwEmpty: pws.length ? !pws[0].e.value : null, alert, alertText, dialog, challenge,
          submitOff: sb ? !!(sb.disabled || sb.getAttribute('aria-disabled') === 'true') : null};
}"""
LOGIN_BUTTONS = "button, input[type=submit], input[type=button], [role=button], a"

# 로그인 화면에 어떤 방법이 있는가: 로그인 화면으로 가는 입구("로그인" 링크·단추)와 소셜 로그인 단추(카카오·네이버·구글·애플).
# 글자·이름표·클래스·그림 이름으로 알아본다. 돌려주는 것은 차례(LOGIN_BUTTONS 가운데 몇 번째)뿐이다.
LOGIN_METHODS_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none' && !e.disabled; };
  const BTN = 'button, input[type=submit], input[type=button], [role=button], a';
  const btns = Array.from(document.querySelectorAll(BTN));
  const txt = b => ((b.innerText || b.value || b.getAttribute('aria-label') || b.getAttribute('title') || '') + '').replace(/\s+/g, ' ').trim();
  const hay = b => { const kids = Array.from(b.querySelectorAll('img, svg title, [class]')).slice(0, 6)
      .map(k => [k.getAttribute('alt'), (k.getAttribute('src') || '').split('?')[0].split('/').pop(), k.getAttribute('class'), k.tagName === 'title' ? k.textContent : ''].join(' '));
    return [txt(b), b.id, b.getAttribute('class'), (b.getAttribute('href') || '').split('?')[0], b.getAttribute('data-provider'), kids.join(' ')].join(' ').toLowerCase(); };
  const WHO = {kakao: /kakao|카카오/, naver: /naver|네이버/, google: /google|구글/, apple: /apple|애플/};
  const NOT = /play|store|스토어|다운로드|download|지도|map|공유|share|채널|channel|상담|블로그|blog|카페|cafe|톡으로 보내|뉴스|news|고객센터/;
  const LOGINISH = /로그인|log ?in|sign ?in|계속|continue|시작|간편/i;
  const providers = [];
  for (const who of Object.keys(WHO)) {
    const c = btns.map((b, i) => ({b, i})).filter(x => vis(x.b) && WHO[who].test(hay(x.b)) && !NOT.test(hay(x.b)) && txt(x.b).length <= 40
      && (LOGINISH.test(txt(x.b)) || txt(x.b).length <= 12));
    c.sort((a, z) => (LOGINISH.test(txt(z.b)) ? 1 : 0) - (LOGINISH.test(txt(a.b)) ? 1 : 0));
    if (c.length) providers.push({who, i: c[0].i, login: LOGINISH.test(txt(c[0].b))});   // login: 글자가 로그인하라는 말인가("카카오로 로그인"). 짧은 이름뿐인 단추("카카오톡")는 아니다
  }
  const hasPw = Array.from(document.querySelectorAll('input[type=password]')).some(vis);
  let entry = -1, entryKind = null;
  if (!hasPw) {
    const all = btns.map((b, i) => ({b, i})).filter(x => vis(x.b));
    const href = b => (b.getAttribute('href') || '').split('?')[0].toLowerCase();
    const byText = all.filter(x => /^(로그인|로그인하기|로그인\/회원가입|로그인 \/ 회원가입|log ?in|sign ?in)$/i.test(txt(x.b)))[0];
    const byHref = all.filter(x => /(^|[\/._-])(login|signin|sign-in)([\/._-]|$)/.test(href(x.b)) && txt(x.b).length <= 20)[0];
    // 내 정보 입구: 로그인하지 않은 채 누르면 로그인 화면으로 보내는 곳이 많다(글자로 된 "로그인"이 없는 사이트 — 실측)
    const byMy = all.filter(x => /^(마이|my|마이페이지|my page|mypage|내 정보|내정보|나의 정보|프로필|profile|account|계정)$/i.test(txt(x.b))
      || (/(^|\/)(my|mypage|member|account|profile)(\/|$)/.test(href(x.b)) && txt(x.b).length <= 12))[0];
    const e = byText || byHref || byMy;
    if (e) { entry = e.i; entryKind = byText ? 'login' : byHref ? 'login_link' : 'my'; }
    else {
      // 단추가 아닌 칸(div·span)에 글만 있고 눌리는 "Sign In"(실기기 2026-11-17 어느 웹앱: 단추·링크가 아니라 글이었다).
      // 눌리는 모양(cursor: pointer)일 때만, 차례 대신 표시(data-laa-entry)를 붙인다
      document.querySelectorAll('[data-laa-entry]').forEach(x => x.removeAttribute('data-laa-entry'));
      const plain = Array.from(document.querySelectorAll('div, span, li, p')).filter(x => vis(x)
        && /^(로그인|로그인하기|log ?in|sign ?in)$/i.test(((x.innerText || '') + '').replace(/\s+/g, ' ').trim())
        && getComputedStyle(x).cursor === 'pointer');
      const inner = plain.find(x => !plain.some(o => o !== x && x.contains(o)));
      if (inner) { inner.setAttribute('data-laa-entry', '1'); entry = -2; entryKind = 'login'; }
    }
  }
  return {providers, entry, entryKind};
}"""


# 칸이 없는 중간 화면(예: "이 계정으로 계속하기", 동의·확인)에서 누를 수 있는 단추의 글. 약관·도움말 같은 바닥 링크는 뺀다.
# 이름 없는 로그인 방법 카드: 그림 카드처럼 누르는 것 자체에는 글이 없고, 옆(뒤따르는 머리글)이나 감싼 칸의 머리글에 방법 이름이 있는 화면
# (실측 2026-11-16, 정부 통합로그인: "모바일 신분증 / 간편 인증 / 공동인증서 / 금융인증서 / 민간ID / 아이디 로그인"이 모두 이름 없는 링크였다).
# 돌려주는 것: 방법 이름과 차례(LOGIN_BUTTONS 가운데 몇 번째). "로그인" 입구·약관·가입 같은 것은 뺀다
LOGIN_CARDS_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none' && !e.disabled; };
  const BTN = 'button, input[type=submit], input[type=button], [role=button], a';
  const t = e => ((e.innerText || e.value || '') + '').replace(/\s+/g, ' ').trim();
  // ID는 낱말일 때만(대소문자 그대로) — /ID/i는 "Slide"에 걸렸다(실기기 2026-11-17: 배너의 "Slide 1~5"가 방법 카드로 나왔다)
  const WORD = /로그인|인증|신분증|아이디|(^|[^A-Za-z])ID([^A-Za-z]|$)/i;
  const SLIDE = /^(go to )?(slide|슬라이드)\s*\d+( of \d+)?$|^\d+\s*\/\s*\d+$/i;
  const NOT = /약관|개인정보|고객센터|도움말|회원가입|사용자 등록|사용자 관리|찾기|안내|가이드|소개|로그아웃|바로가기/;
  const out = [];
  Array.from(document.querySelectorAll(BTN)).forEach((b, i) => {
    if (!vis(b)) return;
    const inner = b.querySelector('h1, h2, h3, h4, h5, h6, strong, dt');   // 카드 안에 머리글과 설명이 함께 든 꼴: 머리글이 방법 이름이다
    let label = inner && t(inner) ? t(inner) : t(b);
    if (inner && t(inner)) { /* 카드 */ }
    else if (!label) {
      const n = b.nextElementSibling;
      if (n && /^H[1-6]$/.test(n.tagName)) label = t(n);
      if (!label) { const box = b.closest('li, article, section, div'); const h = box && box.querySelector('h1, h2, h3, h4, h5, h6, strong, dt');
        if (h && box.querySelectorAll(BTN).length <= 2) label = t(h); }
      if (!label) { const im = b.querySelector('img'); label = ((b.getAttribute('title') || b.getAttribute('aria-label') || (im && im.alt) || '') + '').trim(); }
    } else if (!/btn|button|card|login|cert|auth/i.test((b.getAttribute('class') || '') + ' ' + (b.getAttribute('href') || ''))) return;
    if (!label || label.length > 20 || !WORD.test(label) || NOT.test(label) || SLIDE.test(label)) return;
    if (/^(로그인|로그인하기|로그인\/회원가입|로그인 \/ 회원가입)$/.test(label)) return;
    if (!out.some(x => x.label === label)) out.push({label, i});
  });
  return {cards: out.slice(0, 8)};
}"""


LOGIN_ACTIONS_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none' && !e.disabled; };
  const BTN = 'button, input[type=submit], input[type=button], [role=button], a';
  const txt = b => ((b.innerText || b.value || b.getAttribute('aria-label') || '') + '').replace(/\s+/g, ' ').trim();
  const NOT = /약관|개인정보|고객센터|도움말|help|privacy|terms|policy|한국어|english|日本語|언어|language|©|copyright|회사 ?소개|공지/i;
  const out = [];
  // 슬라이드·배너 넘기기 단추는 로그인 방법이 아니다(실기기 2026-11-17: "Slide 1~5"가 방법 카드에 나왔다)
  const SLIDE = /^(go to )?(slide|슬라이드)\s*\d+( of \d+)?$|^\d+\s*\/\s*\d+$|^(prev|previous|next) slide$|^(이전|다음) 슬라이드$/i;
  const CAROUSEL = '[aria-roledescription=carousel], [aria-roledescription=slide], .swiper, .swiper-pagination, .slick-slider, .slick-dots, .carousel, [class*=carousel], [class*=Carousel], [class*=swiper], [class*=pagination]';
  Array.from(document.querySelectorAll(BTN)).forEach((b, i) => {
    const t = txt(b);
    if (!vis(b) || t.length < 2 || t.length > 24 || NOT.test(t) || SLIDE.test(t) || (b.closest(CAROUSEL) && !(b.innerText || '').trim())) return;   // 넘기기 점(글 없는 단추)만 — 슬라이드 안의 진짜 단추는 남긴다
    if (b.tagName === 'A' && !(b.getAttribute('role') === 'button' || /btn|button/i.test(b.getAttribute('class') || '') || /로그인|계속|확인|동의|허용|취소|다음|continue|allow|cancel|next|ok/i.test(t))) return;
    if (!out.some(x => x.t === t)) out.push({t, i});
  });
  // 간편인증의 "인증 완료"는 단추가 많은 화면(인증 기관 13곳)의 맨 아래에 있다: 다섯 개 안에 들게 앞으로
  out.sort((a, z) => (/^인증\s*완료$/.test(z.t) ? 1 : 0) - (/^인증\s*완료$/.test(a.t) ? 1 : 0));
  return {buttons: out.slice(0, 5)};
}"""


# QR 로그인 화면(D85): "QR코드 로그인" 같은 탭과, QR 그림(네모난 img·canvas·svg), 다시 받기 단추에 표시(data-laa-qr)를 붙이고 꼴만 돌려준다.
# 그림은 사람에게 보여 주려고 찍는다(해독하지 않는다). 숫자는 그림 둘레의 글에 하나만 있을 때만 읽는다
QR_FORM_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e); return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const txt = e => ((e && (e.innerText || e.textContent || e.value)) || '').replace(/\s+/g, ' ').trim();
  document.querySelectorAll('[data-laa-qr]').forEach(e => e.removeAttribute('data-laa-qr'));
  const hint = e => { let h = ''; for (let n = e, i = 0; n && n.getAttribute && i < 4; n = n.parentElement, i++)
      h += ' ' + (n.getAttribute('class') || '') + ' ' + (n.id || '') + ' ' + (n.getAttribute('alt') || '') + ' ' + (n.getAttribute('aria-label') || '')
        + ' ' + ((n.getAttribute('src') || '').slice(0, 80)); return h; };
  const square = e => { const r = e.getBoundingClientRect(); return r.width >= 100 && r.height >= 100 && r.width <= 600 && Math.abs(r.width - r.height) <= Math.max(8, r.width * 0.12); };
  const pics = Array.from(document.querySelectorAll('img, canvas, svg')).filter(e => vis(e) && square(e));
  const pageHasQr = /QR/i.test(txt(document.body).slice(0, 4000));
  let img = pics.find(e => /qr/i.test(hint(e)));
  if (!img && pageHasQr) img = pics.find(e => e.tagName === 'CANVAS' || e.tagName === 'svg' || e.tagName === 'SVG' || /^data:image/.test(e.getAttribute('src') || ''));
  const clickable = Array.from(document.querySelectorAll('button, a, [role=button], [role=tab], li, label, input[type=button]')).filter(vis);
  const tabs = clickable.filter(e => { const t = txt(e); return t.length >= 2 && t.length <= 20 && /QR/i.test(t) && !/안내|도움말|방법|설치|다운/.test(t); });
  const tabEl = tabs.find(e => !tabs.some(o => o !== e && e.contains(o)));   // 가장 안쪽 것(줄 전체가 아니라 그 안의 링크)
  let around = '', number = null, expired = false, refresh = null;
  if (img) {
    img.setAttribute('data-laa-qr', 'img');
    let box = img; for (let i = 0; i < 3 && box.parentElement && txt(box).length < 20; i++) box = box.parentElement;
    if (txt(box).length < 20 && box.parentElement) box = box.parentElement;
    around = txt(box).slice(0, 300);
    const nums = (around.replace(/\d{1,2}\s*:\s*\d{2}/g, ' ').replace(/\d+\s*(분|초|시간|일|단계|번째|자리)/g, ' ').match(/(^|[^\d.,])(\d{1,3})(?![\d.,])/g) || [])
      .map(x => x.replace(/[^\d]/g, ''));
    if (nums.length === 1) number = nums[0];
    expired = /만료|시간이 지났|시간이 초과|시간 초과|유효하지|expired/i.test(around);
    const rbs = clickable.filter(e => { const t = txt(e); return t.length >= 2 && t.length <= 16 && /새로 ?고침|다시 ?받기|재발급|갱신|다시 ?시도|재시도|refresh|retry/i.test(t); });
    const rb = rbs.find(e => !rbs.some(o => o !== e && e.contains(o)));
    if (rb) { rb.setAttribute('data-laa-qr', 'refresh'); refresh = txt(rb); }
  }
  if (tabEl) tabEl.setAttribute('data-laa-qr', 'tab');
  return {tab: tabEl ? txt(tabEl) : null, shown: !!img, around, number, expired, refresh};
}"""


# 간편인증 입력 화면(실측 2026-11-16, 정부 통합로그인의 간편인증 창): 인증 기관을 고르고 이름·생년월일·휴대폰 번호를 넣고
# 동의에 표시한 뒤 "인증 요청"을 누른다. 칸·기관·동의 항목에 표시(data-laa-easy)를 붙이고 꼴만 돌려준다. 값은 읽지 않는다
EASY_FORM_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e); return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const txt = e => ((e && (e.innerText || e.textContent)) || '').replace(/\s+/g, ' ').trim();
  const labelOf = e => {
    const parts = [e.getAttribute('aria-label'), e.getAttribute('title'), e.getAttribute('placeholder')];
    if (e.id) { try { const l = document.querySelector('label[for="' + CSS.escape(e.id) + '"]'); if (l) parts.push(txt(l)); } catch (x) {} }
    const w = e.closest('label'); if (w) parts.push(txt(w));
    const lb = e.getAttribute('aria-labelledby'); if (lb) parts.push(lb.split(/\s+/).map(i => txt(document.getElementById(i))).join(' '));
    return parts.filter(Boolean).join(' ').replace(/\s+/g, ' ').trim().slice(0, 80);
  };
  document.querySelectorAll('[data-laa-easy]').forEach(e => e.removeAttribute('data-laa-easy'));
  const fields = []; let rrn = false, other = 0;
  Array.from(document.querySelectorAll('input')).forEach(e => {
    const t = (e.getAttribute('type') || 'text').toLowerCase();
    if (!['text', 'tel', 'number', 'password', 'search'].includes(t) || !vis(e) || e.disabled || e.readOnly) return;
    const l = labelOf(e);
    let role = null;
    if (/주민|외국인등록/.test(l)) { rrn = true; return; }
    if (/생년월일|생일/.test(l)) role = 'birth';
    else if (/휴대폰|휴대전화|핸드폰|전화번호/.test(l)) role = 'phone';
    else if (/이름|성명/.test(l)) role = 'name';
    if (!role || fields.some(f => f.role === role)) { other++; return; }
    e.setAttribute('data-laa-easy', 'f-' + role);
    fields.push({role, label: l.slice(0, 30)});
  });
  let prefix = [];
  const sel = Array.from(document.querySelectorAll('select')).find(e => vis(e) && Array.from(e.options).some(o => o.value === '010' || txt(o) === '010'));
  if (sel) { sel.setAttribute('data-laa-easy', 'prefix'); prefix = Array.from(sel.options).map(o => txt(o) || o.value).filter(v => /^\d{3}$/.test(v)); }
  // 인증 기관: 그림(alt)과 짧은 이름이 든 누르는 것. 안쪽 것만(겹쳐 세지 않는다)
  const cand = Array.from(document.querySelectorAll('a, button, [role=button], [role=radio], li, label')).filter(e => {
    if (!vis(e) || !e.querySelector('img[alt]')) return false;
    const t = txt(e); return t.length >= 2 && t.length <= 14 && e.querySelectorAll('img').length === 1 && !/안내|매뉴얼|도움말|닫기|보기|요청|동의/.test(t);
  });
  const inner = cand.filter(e => !cand.some(o => o !== e && e.contains(o)));
  const providers = [];
  inner.forEach(e => { const t = txt(e); if (!providers.some(p => p.label === t)) { e.setAttribute('data-laa-easy', 'p-' + providers.length); providers.push({label: t}); } });
  // 동의 항목: 체크 칸(꾸민 칸은 input이 숨어 있고 label이 보인다)
  const consents = []; let all = false;
  Array.from(document.querySelectorAll('input[type=checkbox]')).forEach(e => {
    const l = labelOf(e).replace(/\s*보기\s*$/, '').trim();
    const lab = (e.id && document.querySelector('label[for="' + CSS.escape(e.id) + '"]')) || e.closest('label');
    if (!/동의/.test(l) || !(vis(e) || (lab && vis(lab)))) return;
    if (/전체/.test(l)) { all = true; return; }
    if (consents.some(c => c.label === l)) return;
    e.setAttribute('data-laa-easy', 'c-' + consents.length);
    if (lab) lab.setAttribute('data-laa-easy', 'cl-' + consents.length);
    consents.push({label: l.slice(0, 40), required: !/선택/.test(l)});
  });
  const BTN = 'button, input[type=submit], input[type=button], [role=button], a';
  const req = Array.from(document.querySelectorAll(BTN)).find(b => vis(b) && /^인증\s*요청$/.test(((b.innerText || b.value || '') + '').replace(/\s+/g, ' ').trim()));
  if (req) req.setAttribute('data-laa-easy', 'submit');
  return {fields, prefix, providers: providers.slice(0, 20), consents: consents.slice(0, 8), all, rrn, other, submit: !!req};
}"""


# 로그인 화면 위에 뜬 확인 문제(보안문자) 층: 그 상자와, 그 안의 답 칸·누를 단추에 표시를 붙이고 꼴을 돌려준다. 풀지 않는다 — 사람에게 보여 주고 답을 받는다.
CHALLENGE_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e); return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  document.querySelectorAll('[data-laa-ch]').forEach(e => e.removeAttribute('data-laa-ch'));
  const boxes = Array.from(document.querySelectorAll('[role=dialog], [role=alertdialog], [aria-modal="true"], dialog[open], [class*="captcha" i], [id*="captcha" i], [class*="layer" i][class*="quiz" i]'))
    .filter(e => e.tagName !== 'INPUT' && e.tagName !== 'IMG' && vis(e) && e.getBoundingClientRect().width >= 120 && e.getBoundingClientRect().height >= 80);
  if (!boxes.length) return {present: false};
  // 가장 바깥의 것(다른 후보를 품고 있는 것)을 고른다
  const box = boxes.filter(b => !boxes.some(o => o !== b && o.contains(b)))[0] || boxes[0];
  box.setAttribute('data-laa-ch', 'box');
  const inputs = Array.from(box.querySelectorAll('input, textarea')).filter(e => vis(e) && !e.disabled && !e.readOnly
    && ['text', 'tel', 'number', 'search', ''].includes((e.getAttribute('type') || 'text').toLowerCase()));
  if (inputs.length === 1) inputs[0].setAttribute('data-laa-ch', 'input');
  const txt = b => ((b.innerText || b.value || b.getAttribute('aria-label') || '') + '').replace(/\s+/g, ' ').trim();
  const btns = Array.from(box.querySelectorAll('button, input[type=submit], input[type=button], [role=button], a')).filter(e => vis(e) && !e.disabled);
  // 답을 내는 단추: 이름이 딱 맞는 것이 먼저, 없으면 '제출·확인·인증'이 든 이름("답변 제출" — 실기기에서 이 이름이라 못 눌렀다). 새로 풀기·음성·닫기·도움말은 아니다
  const NOTOK = /새로|다시|음성|재생|닫기|취소|도움말|help|refresh|reload|audio|close|cancel/i;
  const ok = btns.filter(b => /^(확인|제출|다음|완료|답변하기|답변|전송|입력|인증|ok|submit|verify|next|continue|done)$/i.test(txt(b)))[0]
    || btns.filter(b => /제출|확인|인증|submit|verify|confirm/i.test(txt(b)) && !NOTOK.test(txt(b)) && txt(b).length <= 20)[0]
    || btns.filter(b => (b.getAttribute('type') || '').toLowerCase() === 'submit' && !NOTOK.test(txt(b)))[0];
  if (ok) ok.setAttribute('data-laa-ch', 'ok');
  const label = inputs.length === 1 ? ((inputs[0].getAttribute('aria-label') || inputs[0].getAttribute('placeholder') || '') + '').trim().slice(0, 40) : '';
  const hasFrame = Array.from(box.querySelectorAll('iframe')).some(vis);
  return {present: true, text: (box.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 300), inputs: inputs.length, ok: !!ok, label, frame: hasFrame,
          pictures: Array.from(box.querySelectorAll('img, canvas, svg')).filter(vis).length};
}"""


# 확인 문제가 별도 틀(iframe) 안에 있을 때 그 틀 안에서 답 칸·확인 단추를 찾는다(꼴만 돌려준다)
CHALLENGE_FRAME_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e); return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  document.querySelectorAll('[data-laa-ch]').forEach(e => e.removeAttribute('data-laa-ch'));
  const inputs = Array.from(document.querySelectorAll('input, textarea')).filter(e => vis(e) && !e.disabled && !e.readOnly
    && ['text', 'tel', 'number', 'search', ''].includes((e.getAttribute('type') || 'text').toLowerCase()));
  if (inputs.length === 1) inputs[0].setAttribute('data-laa-ch', 'input');
  const txt = b => ((b.innerText || b.value || b.getAttribute('aria-label') || '') + '').replace(/\s+/g, ' ').trim();
  const btns = Array.from(document.querySelectorAll('button, input[type=submit], input[type=button], [role=button], a')).filter(e => vis(e) && !e.disabled);
  // 답을 내는 단추: 이름이 딱 맞는 것이 먼저, 없으면 '제출·확인·인증'이 든 이름("답변 제출" — 실기기에서 이 이름이라 못 눌렀다). 새로 풀기·음성·닫기·도움말은 아니다
  const NOTOK = /새로|다시|음성|재생|닫기|취소|도움말|help|refresh|reload|audio|close|cancel/i;
  const ok = btns.filter(b => /^(확인|제출|다음|완료|답변하기|답변|전송|입력|인증|ok|submit|verify|next|continue|done)$/i.test(txt(b)))[0]
    || btns.filter(b => /제출|확인|인증|submit|verify|confirm/i.test(txt(b)) && !NOTOK.test(txt(b)) && txt(b).length <= 20)[0]
    || btns.filter(b => (b.getAttribute('type') || '').toLowerCase() === 'submit' && !NOTOK.test(txt(b)))[0];
  if (ok) ok.setAttribute('data-laa-ch', 'ok');
  return {ready: true, text: (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').trim().slice(0, 300), inputs: inputs.length, ok: !!ok,
          label: inputs.length === 1 ? ((inputs[0].getAttribute('aria-label') || inputs[0].getAttribute('placeholder') || '') + '').trim().slice(0, 40) : '',
          buttons: btns.length, pictures: Array.from(document.querySelectorAll('img, canvas, svg')).filter(vis).length,
          clickables: Array.from(document.querySelectorAll('[onclick], [role=checkbox], [role=radio], input[type=checkbox], input[type=radio], li, td')).filter(vis).length};
}"""


def start_display() -> bool:
    """BROWSER_HEADED=1이면 컨테이너 안에 가상 화면(Xvfb)을 띄운다 → 브라우저를 창 있는 모드로 띄울 수 있다.

    창 없는 모드는 스스로를 HeadlessChrome이라고 밝혀 여러 사이트가 첫 화면부터 거른다(2026-11-15 측정).
    값을 속이는 것이 아니다: 실제로 창 있는 브라우저이고, 자동화 표시(navigator.webdriver)는 그대로 둔다.
    가상 화면을 못 띄우면 False — 창 없는 모드로 간다.
    """
    if os.environ.get("BROWSER_HEADED") != "1":
        return False
    import subprocess
    import time
    try:
        proc = subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    import socket
    for _ in range(60):            # 화면이 연결을 받을 때까지(최대 3초). 소켓 파일이 생긴 것만으로는 아직 못 받는다
        if proc.poll() is not None:
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
                c.settimeout(0.2)
                c.connect("/tmp/.X11-unix/X99")
            time.sleep(0.2)
            os.environ["DISPLAY"] = ":99"
            return True
        except OSError:
            time.sleep(0.05)
    proc.kill()
    return False


class Browser:
    def __init__(self, pw):
        # 전용 크롬 길(BROWSER_REAL_PROFILE): 컨테이너가 아니라 맥에서, 깔려 있는 진짜 크롬을 에이전트 전용 프로필로 띄운다.
        # 로그인은 사람이 그 프로필에서 손으로 해 둔다(자동화된 브라우저의 로그인을 받지 않는 회사 — 구글). 자동화 표시는 그대로 둔다.
        real = os.environ.get("BROWSER_REAL_PROFILE")
        self.headed = True if real else start_display()
        # 가상 화면의 DISPLAY는 Playwright가 뜬 뒤에 정해졌으므로 브라우저에 환경을 직접 넘긴다
        mode = {"headless": False, "env": dict(os.environ)} if self.headed else {}
        # PROFILE_DIR이 있으면 사용자별 영구 프로필(쿠키·로그인 세션 유지, Phase 5), 없으면 일회용 컨텍스트.
        profile = os.environ.get("PROFILE_DIR")
        # 시간대: 사용자의 컴퓨터 것(BROWSER_TZ, 코드가 넘긴 값). 컨테이너의 UTC 그대로면 사이트의 시각이 어긋난다. 꼴이 이상하면 쓰지 않는다
        tz = os.environ.get("BROWSER_TZ") or ""
        zone = {"timezone_id": tz} if re.fullmatch(r"[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}", tz) else {}
        # 컨테이너 안에서 쓸 브라우저: 기본은 Playwright의 Chromium, BROWSER_CHANNEL=chrome이면 이미지에 깔린 구글 크롬(D86). 자동화 표시는 어느 쪽이든 그대로다
        chan = {"channel": "chrome"} if not real and os.environ.get("BROWSER_CHANNEL") == "chrome" else {}
        if real:
            extra = [a for a in os.environ.get("BROWSER_EXTRA_ARGS", "").split("\n") if a.startswith("--")]
            self.browser = None
            self.context = pw.chromium.launch_persistent_context(
                real, channel=os.environ.get("BROWSER_CHANNEL") or "chrome", headless=False, accept_downloads=False,
                # Playwright는 맥에서 크롬을 가짜 키체인(--use-mock-keychain, --password-store=basic)으로 띄운다. 그러면 사람이 보통의 크롬 창에서
                # 로그인해 둔 쿠키(진짜 키체인의 열쇠로 잠겨 있다)를 읽지 못해 늘 로그아웃으로 보인다(실기기 2026-11-16: 구글에 로그인해 두었는데
                # 에이전트가 연 유튜브는 로그아웃이었다). 이 둘만 뺀다 — 자동화 표시(--enable-automation 등)는 그대로 둔다.
                ignore_default_args=["--use-mock-keychain", "--password-store=basic"],
                # 이것을 주지 않으면 Playwright가 --no-sandbox를 붙이고, 진짜 크롬은 창 맨 위에 "지원되지 않는 명령줄 플래그" 줄을 띄운다
                chromium_sandbox=True,
                # 화면 크기: 1280x720이면 무거운 웹앱의 설정 줄이 잘려 보이지 않았다(2026-11-17)
                viewport={"width": 1600, "height": 1000},
                args=["--no-first-run", "--no-default-browser-check", *extra])
        elif profile:
            # 이전 컨테이너가 비정상 종료하며 남긴 잠금 파일 제거. 사용자당 컨테이너 하나는 Sandbox Manager가 보장한다.
            for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                try:
                    os.unlink(os.path.join(profile, name))
                except OSError:
                    pass
            self.browser = None
            self.context = pw.chromium.launch_persistent_context(
                profile, chromium_sandbox=True, **chan, accept_downloads=DOWNLOADS, locale="ko-KR", **zone, **mode)
        else:
            self.browser = pw.chromium.launch(chromium_sandbox=True, **chan, **mode)
            self.context = self.browser.new_context(accept_downloads=DOWNLOADS, locale="ko-KR", **zone)
        self.context.set_default_timeout(ACTION_TIMEOUT_MS)
        self.context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.refmap: dict[str, str] = {}  # 모델용 ref → Playwright ref (마지막 관찰 결과 기준)
        self._revived = False   # 이 주소에서 멈춘 페이지를 이미 한 번 새로 열었는가(navigate마다 되돌린다)
        self._opener = None     # 로그인 팝업에서 이어 가는 동안의 원래 탭
        # 확인 창(alert·confirm): 기본은 닫기(취소). 설정 흐름이 '저장'·'생성' 직전에만 수락으로 바꾼다(dialog_policy)
        self.accept_dialogs = False
        self.last_dialog: str | None = None
        self.page.on("dialog", self._on_dialog)
        # 팝업·새 탭: 탭은 하나만 다룬다. 누르기가 연 새 탭은 click이 그 주소를 이 탭에서 열고(_adopt_popup), 그 밖에 열린 것은 닫는다
        self._popup = None
        self.context.on("page", self._on_page)

    def _on_page(self, p):
        if p is self.page or p is getattr(self, "_opener", None) or getattr(self, "_reviving", False):   # 멈춘 탭을 대신할 새 탭은 팝업이 아니다
            return
        if self._popup is None:
            self._popup = p          # click이 곧 거둬 간다
        else:
            try:
                p.close()
            except PlaywrightError:
                pass

    def _on_dialog(self, dialog):
        self.last_dialog = f"{dialog.type}: {dialog.message[:200]}"
        accept = bool(self.accept_dialogs) or dialog.type == "alert"   # alert는 '확인'뿐이다
        # 방금 한 동작이 띄운 창(동작의 결과에 실어 알린다): 종류·글·어떻게 닫았는지
        self._dialog_seen = {"type": dialog.type, "message": dialog.message[:300], "accepted": accept and dialog.type != "alert"}
        try:
            dialog.accept() if accept else dialog.dismiss()
        except PlaywrightError:
            pass

    def _acting(self, args):
        """동작 하나를 하는 동안: 승인된 동작이면(accept_dialog) 그것이 띄우는 확인 창을 '확인'으로 닫는다. 띄운 창을 결과에 싣는다."""
        browser = self

        class _Ctx:
            def __enter__(self_inner):
                browser._dialog_seen = None
                self_inner.before = browser.accept_dialogs
                if args.get("accept_dialog") is True:
                    browser.accept_dialogs = True
                return self_inner

            def __exit__(self_inner, *exc):
                browser.accept_dialogs = self_inner.before
                return False
        return _Ctx()

    def _dialog_result(self) -> dict:
        try:
            self.page.wait_for_timeout(150)   # 창은 누른 직후에 뜬다
        except PlaywrightError:
            pass
        seen, self._dialog_seen = getattr(self, "_dialog_seen", None), None
        if seen:
            self.last_dialog = None   # 결과에 실어 알렸다: 관찰 결과의 안내에 되풀이하지 않는다
        return {"dialog": seen} if seen else {}

    def _state(self) -> dict:
        # 제목은 페이지에 물어야 안다 — 스크립트가 멈춘 페이지에서는 그 물음이 끝없이 기다린다(시간 한도가 없다). 먼저 답하는지 본다
        return {"url": self.page.url, "title": self.page.title() if self._awake(1500) else ""}

    def _loc(self, ref: str):
        pw_ref = self.refmap.get(ref)
        if pw_ref is None:
            raise BadRequest(f"현재 관찰 결과에 없는 ref: {ref} (observe를 다시 한다)")
        return self.page.locator(f"aria-ref={pw_ref}")

    def _awake(self, timeout_ms: int = 2500) -> bool:
        """페이지의 스크립트가 지금 답하는가. 주 스레드를 놓지 않는 페이지에서는 읽기 동작이 모두 끝없이 기다린다."""
        # 페이지의 본문 문맥이 아니라 도구의 격리된 문맥에서 기다린다: 옛 사이트는 내장 객체(Map 등)를 자기 것으로 바꿔 놓아
        # 본문 문맥의 기다리기(wait_for_function)가 곧바로 깨진다 — 그것을 "멈췄다"로 잘못 읽었다(실측 2026-11-15, 도서관 사이트: `Map = function(){…}`).
        # 주 스레드가 정말 멈췄으면 이 기다리기도 시간이 다 된다(같은 스레드다)
        try:
            self.page.locator("html").first.wait_for(state="attached", timeout=timeout_ms)
            return True
        except PlaywrightError:
            return False

    def _js(self, js: str, arg=None, frame=None, alive_ms: int = 2500):
        """화면에서 함수 하나를 돌려 그 값을 받는다. 값은 화면 안에서 JSON 글로 바꿔 받는다.

        왜 이렇게: 내장 객체를 바꿔 놓은 사이트에서는 도구의 기다리기(wait_for_function)·선택자 평가가 깨지고, 평가가 돌려준 객체도 None으로 온다.
        한 번에 평가하고 글(문자열)로 받는 길은 그런 화면에서도 된다. 멈춘 화면에서 끝없이 기다리지 않도록 먼저 살아 있는지 본다(시간 한도).
        """
        if not self._awake(alive_ms):
            raise PlaywrightError("Timeout: 페이지가 응답하지 않는다")
        f = frame or self.page.main_frame
        out = f.evaluate("a => { const r = (" + js + ")(a); return r === undefined ? 'null' : JSON.stringify(r); }", arg)
        return json.loads(out) if isinstance(out, str) else None

    def _revive(self) -> bool:
        """읽기 전에: 페이지가 답하지 않으면 **한 번** 새로 연다. 새로 열었으면 True.

        실측(2026-11-15): 처음 받을 때(캐시가 비어 있을 때)만 화면 애니메이션 코드가 쉬지 않고 돌아 멈추고, 다시 열면 정상인 사이트가 있다.
        사이트가 막은 것이 아니라 페이지가 멈춘 것이라, 사람이 새로 고침하듯 한 번만 다시 연다. 그래도 답하지 않으면 오류로 알린다.
        """
        if self._awake():
            return False
        # 무거운 화면은 처음 그리는 몇 초 동안 답하지 않을 뿐일 수 있다(실측 2026-11-15: 도서관 사이트를 "멈췄다"고 보고 새로 열었고, 새로 연 것도 같은 까닭으로 버렸다)
        # → 새로 열기 전에 한 번 더 길게 기다린다
        if self._awake(6000):
            return False
        if self._revived:
            raise BadRequest("페이지가 응답하지 않는다(스크립트가 멈춰 있다). 새로 열어도 같다")
        self._revived = True
        url, old = self.page.url, self.page
        # 멈춘 탭은 새로 고침도 받지 못할 수 있다(주 스레드가 완전히 막힌 경우) → 새 탭으로 열고 멈춘 탭을 닫는다. 쿠키는 그대로다(같은 컨텍스트)
        self._reviving = True
        try:
            fresh = self.context.new_page()
        finally:
            self._reviving = False
        fresh.on("dialog", self._on_dialog)
        self.page = fresh
        try:
            old.close(run_before_unload=False)
        except PlaywrightError:
            pass
        self.refmap = {}
        try:
            self.page.goto(url, wait_until="commit")
        except PlaywrightError:
            raise BadRequest("페이지가 응답하지 않는다(스크립트가 멈춰 있다). 새로 열지 못했다") from None
        self.page.wait_for_timeout(1500)
        if not self._awake(4000):
            raise BadRequest("페이지가 응답하지 않는다(스크립트가 멈춰 있다). 새로 열어도 같다")
        return True

    def _read(self, fn):
        """읽기 동작을 한다 → (결과, 새로 열었는가). 하기 전이나 하는 도중에 페이지가 멈추면 한 번 새로 열고 다시 한다."""
        revived = self._revive()
        try:
            return fn(), revived
        except PlaywrightError:
            if self._awake():
                raise          # 페이지는 살아 있다: 다른 까닭의 오류
            self._revive()     # 읽는 도중에 멈췄다(이미 한 번 새로 열었으면 여기서 오류로 끝난다)
            return fn(), True

    def _settle(self):
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except PlaywrightError:
            pass

    def navigate(self, args):
        url = _str(args, "url", 2048)
        if urlsplit(url).scheme not in ("http", "https"):
            raise BadRequest("http/https URL만 허용")
        opener = getattr(self, "_opener", None)
        if (getattr(self, "keep_popups", False) and getattr(self, "_kept", 0)
                and opener is not None and not opener.is_closed() and not self.page.is_closed()):
            # 그대로 쓰던 새 창(주소 찾기 등)에서 다른 곳으로 가려 한다: 그 창을 닫고 원래 탭에서 간다. 이름 붙은 창이 남아 있으면
            # 원래 화면이 다시 그 창을 열 때 새 창이 뜨지 않는다(실기기 2026-11-18 에어코리아: 주소 찾기 창 안에서 다른 주소로 간 뒤 Enter에 창이 안 떴다)
            try:
                self.page.close()
            except PlaywrightError:
                pass
            self.page, self._opener = opener, None
        resp = self.page.goto(url)
        self.refmap = {}
        self._revived = False
        return {**self._state(), "status": resp.status if resp else None}

    def _password_values(self) -> list[str]:
        try:
            vals = self._js("() => Array.from(document.querySelectorAll('input[type=password]')).map(e => e.value)")
        except PlaywrightError:
            vals = []
        return [v for v in (vals or []) if isinstance(v, str) and v]

    def _text_len(self) -> int:
        n = 0
        for fr in self.page.frames:
            try:
                n += fr.evaluate("() => (document.body && document.body.innerText || '').trim().length")
            except PlaywrightError:
                continue
        return n

    def _quiet(self, max_s: float = 2.5) -> None:
        """읽기 전에 화면이 불러오는 중이면 잠깐 기다린다(D121, 실기기 KOSIS: '조회'를 누른 뒤 표를 새로 불러오는 동안 읽어 숫자가 없었다).
        늘 무엇을 주고받는 사이트(광고 등)도 있어 길게 기다리지 않는다."""
        try:
            self.page.wait_for_load_state("networkidle", timeout=int(max_s * 1000))
        except PlaywrightError:
            pass

    def _wait_thin(self, max_s: float = 6.0) -> None:
        """화면에 글이 거의 없으면(아직 그리는 중) 잠깐 기다린다(D121, 실기기 2026-11-18 KOSIS: 표 보기 창이 몇 초에 걸쳐 그려지는데
        빈 화면을 모델에게 넘겨 10초 만에 포기했다). 글이 충분한 화면은 기다리지 않는다."""
        try:
            if self._text_len() >= THIN_CHARS:
                return
            # 불러오는 중이면(네트워크가 바쁘다) 조용해질 때까지 — 이미 조용한 작은 화면은 0.5초쯤이면 지나간다
            self.page.wait_for_load_state("networkidle", timeout=int(max_s * 1000))
        except PlaywrightError:
            return

    def observe(self, args):
        self._wait_thin()
        snap, revived = self._read(lambda: self.page.locator("body").first.aria_snapshot(mode="ai"))
        # 접근성 스냅샷에는 비밀번호 칸의 값이 평문으로 나온다(1.64.1 실측) → 모델·로그로 나가기 전에 가린다.
        for v in sorted(self._password_values(), key=len, reverse=True):
            snap = snap.replace(v, "••••")
        st = self._state()
        text, self.refmap = format_observation_with_refs(st["url"], st["title"], snap)
        text = self._editable_values(text)
        notices = []
        try:   # 화면 위의 안내 창 글(배열은 비어도 참이라 바로 돌아온다). 못 읽어도 관찰 결과는 준다
            got = self._js(NOTICE_JS)
            notices = [str(x)[:300] for x in got][:6] if isinstance(got, list) else []
        except PlaywrightError:
            pass
        if self.last_dialog:   # 브라우저 확인 창(alert·confirm)으로 뜬 글도(지우지 않고 본다)
            notices.append(self.last_dialog[:300])
        return {**st, "text": text, "notices": notices, **({"revived": True} if revived else {})}

    def _editable_values(self, text: str) -> str:
        """직접 편집하는 영역(contenteditable인 textbox)에 든 글을 값으로 붙인다.

        스냅샷은 그런 영역의 글을 값으로 주지 않는다 — `textbox "메모"`로만 나온다(2026-11-15 합성 플래너: 메모를 저장해 놓고도 다시 열었을 때
        "비어 있다"고 보고 저장을 되풀이했다). 실제 서비스의 편집기·제목 칸이 이 방식이다. 값이 안 붙은 textbox 줄만, 12개까지 본다.
        """
        lines = text.split("\n")
        todo = [(i, m.group(1)) for i, l in enumerate(lines) if (m := re.match(r'^\[(e\d+)\] textbox "[^"]*"$', l))][:12]
        for i, ref in todo:
            try:
                v = self._loc(ref).evaluate("e => e.isContentEditable ? (e.innerText || '') : ''", timeout=1000)
            except (PlaywrightError, BadRequest):
                continue
            v = " ".join(str(v).split())
            if v:
                lines[i] += ' = "' + (v[:200] + ("…" if len(v) > 200 else "")).replace('"', "'") + '"'
        return "\n".join(lines)

    def click(self, args):
        """요소를 누른다. 실제 사이트에서는 팝업 층에 가려지거나 화면 밖에 있어 보통의 누르기가 시간 초과가 된다(2026-11-14 측정:
        조회 화면 과제의 실패가 모두 이것이었다) → 화면 안으로 옮겨 다시 누르고, 그래도 안 되면 요소에 직접 누르기 이벤트를 보낸다.
        how: 어떻게 눌렀는지(click | scrolled | script). 새 탭으로 열리는 링크는 그 주소를 이 탭에서 연다(탭은 하나만 다룬다)."""
        ref = _ref(args)
        loc = self._loc(ref)
        self._popup = None
        how = "click"
        with self._acting(args):
            try:
                loc.click(timeout=4000)
            except PlaywrightError as e:
                if "Timeout" not in str(e):
                    raise
                try:
                    loc.scroll_into_view_if_needed(timeout=2000)
                    loc.click(timeout=2500)
                    how = "scrolled"
                except PlaywrightError:
                    loc.evaluate("e => e.click()")
                    how = "script"
            self._adopt_popup()
            self._settle()
            dlg = self._dialog_result()
        return {**self._state(), "how": how, **dlg}

    def press_key(self, args):
        """입력 칸에서 키 하나를 누른다(Enter·Escape·Tab·화살표). Enter가 검색창이 아닌 칸에서 눌릴 때의 승인은 호스트가 맡는다."""
        key = args.get("key")
        if key not in ("Enter", "Escape", "Tab", "ArrowDown", "ArrowUp"):
            raise BadRequest("key: Enter | Escape | Tab | ArrowDown | ArrowUp")
        loc = self._loc(_ref(args))
        with self._acting(args):
            loc.press(key, timeout=4000)
            self._settle()
            dlg = self._dialog_result()
        return {**self._state(), **dlg}

    def hover(self, args):
        """요소 위에 마우스를 올린다(올려야 보이는 단추·메뉴)."""
        self._loc(_ref(args)).hover(timeout=4000)
        try:
            self.page.wait_for_timeout(250)
        except PlaywrightError:
            pass
        return self._state()

    def drag(self, args):
        """화면 글로 찾은 것을 끌어, ref로 준 곳에 놓는다. 끌 것은 이름표가 없는 경우가 많아(카드) 글로 찾는다 — 글이 정확히 같은 것 가운데 보이는 첫 번째."""
        text = _str(args, "text", 200)
        to = args.get("to_ref")
        if not isinstance(to, str) or not REF.match(to):
            raise BadRequest("to_ref: 놓을 곳의 ref")
        src = self.page.get_by_text(text, exact=True)
        n = src.count()
        if n == 0:
            raise BadRequest(f"화면에서 '{text[:40]}' 글을 찾지 못했다(보이는 글 그대로 써야 한다)")
        src.first.drag_to(self._loc(to), timeout=6000)
        self._settle()
        try:
            self.page.wait_for_timeout(300)
        except PlaywrightError:
            pass
        return {**self._state(), "matched": n}

    def _adopt_popup(self):
        """방금 누른 것이 새 탭을 열었으면: 그 탭의 주소를 이 탭에서 열고 새 탭은 닫는다."""
        try:
            self.page.wait_for_timeout(300)
        except PlaywrightError:
            pass
        pop, self._popup = self._popup, None
        if pop is None:
            return
        if getattr(self, "keep_popups", False) and not pop.is_closed():
            # 새 창은 그 창 그대로 쓴다(D120) — 주소만 이 탭에서 다시 열면 window.opener가 없어 내용이 안 불러와지는 사이트가 있다졌다
            # (실제 2026-11-18: 영상 요소는 있는데 readyState 0·길이 모름). 창이 닫히면 원래 탭으로 돌아온다(_login_page)
            try:
                pop.wait_for_load_state("domcontentloaded", timeout=6000)
            except PlaywrightError:
                pass
            pop.on("dialog", self._on_dialog)
            if getattr(self, "_opener", None) is None:
                self._opener = self.page
            self.page = pop
            self.refmap = {}
            self._kept = getattr(self, "_kept", 0) + 1
            return
        url = ""
        try:
            pop.wait_for_load_state("domcontentloaded", timeout=4000)
            url = pop.url
        except PlaywrightError:
            url = getattr(pop, "url", "") or ""
        try:
            pop.close()
        except PlaywrightError:
            pass
        if urlsplit(url).scheme in ("http", "https"):
            self._adopted = getattr(self, "_adopted", 0) + 1   # 새 창을 이 탭으로 옮긴 횟수(진단 — 옮기면 window.opener가 없다)
            try:
                self.page.goto(url)
                self.refmap = {}
            except PlaywrightError:
                pass

    def type(self, args):
        ref = _ref(args)
        text = args.get("text")
        if not isinstance(text, str) or len(text) > 5000:
            raise BadRequest("text: 5000자 이하 문자열")
        loc = self._loc(ref)
        if loc.get_attribute("type") == "password":
            raise BadRequest("비밀번호 칸에는 에이전트가 입력하지 않는다 (사용자가 직접 입력)")
        try:   # 칸의 안내 문구(D121, 실기기 에어코리아: 이름표 "검색어 입력 : 새창열림"에 가려 "도로명 또는 동을 입력하세요"가 안 보였다)
            hint = (loc.get_attribute("placeholder", timeout=1000) or "").strip()[:80]
        except PlaywrightError:
            hint = ""
        loc.fill(text)
        if args.get("submit") is True:
            loc.press("Enter")
            self._settle()
        return {**self._state(), **({"hint": hint} if hint else {})}

    def download_table(self, args):
        """D125: 그 단추를 눌러 내려받은 CSV·엑셀을 표로 읽어 둔다 → {table_id, file, columns, rows_total, preview}."""
        if not DOWNLOADS or not getattr(self, "downloads_ok", PUBLIC_DOWNLOADS):
            raise BadRequest("이 작업에서는 파일을 내려받지 않는다")
        import tables
        loc = self._loc(_ref(args))
        before = len(self.context.pages)
        try:   # 호스트와 주고받는 한 번의 한도(30초)보다 짧게 — 넘기면 그 뒤 요청이 엉킨다(실기기 2026-11-18 KOSIS)
            with self.page.expect_download(timeout=15_000) as info:
                loc.click()
            dl = info.value
        except PlaywrightError:
            popped = len(self.context.pages) > before or getattr(self, "_popup", None) is not None
            raise BadRequest("누른 뒤 15초 안에 파일이 오지 않았다. " + (
                "새 창이 떴다 — observe로 그 창을 보고, 파일 형식(CSV·엑셀)을 고른 뒤 마지막 내려받기 단추에 download_table을 다시 쓴다" if popped else
                "파일 형식(CSV·엑셀)을 고르는 창이나 선택지가 떴는지 observe로 본다 — 고른 뒤 마지막 내려받기 단추에 download_table을 다시 쓴다. "
                "로그인·활용신청을 요구하면 받지 못한다")) from None
        name = (dl.suggested_filename or "file")[:120]
        try:
            path = dl.path()
            with open(path, "rb") as f:
                data = f.read(tables.MAX_BYTES + 1)
        finally:
            try:
                dl.delete()
            except PlaywrightError:
                pass
        try:
            cols, rows = tables.parse(name, data)
        except tables.TableError as e:
            raise BadRequest(str(e)) from None
        self._tables = getattr(self, "_tables", {})
        tid = f"t{len(self._tables) + 1}"
        if len(self._tables) >= 5:   # 세션에 다섯 개까지 — 오래된 것부터 버린다
            self._tables.pop(next(iter(self._tables)))
        self._tables[tid] = (cols, rows)
        return {**self._state(), "table_id": tid, "file": name, "columns": cols, "rows_total": len(rows), "preview": rows[:5]}

    def table_query(self, args):
        import tables
        got = getattr(self, "_tables", {}).get(args.get("table_id"))
        if got is None:
            raise BadRequest("그 표가 없다 — 먼저 download_table로 받는다")
        try:
            return tables.query(got[0], got[1], args)
        except (tables.TableError, ValueError, TypeError) as e:
            raise BadRequest(str(e)) from None

    def select_option(self, args):
        ref = _ref(args)
        option = _str(args, "option", 200)
        loc = self._loc(ref)
        if (loc.evaluate("e => e.tagName") or "").upper() != "SELECT":
            # 입력하면 추천 목록이 뜨는 칸(role=combobox인 input)이나 직접 만든 고르는 단추다(합성 측정: 여기서 select_option만 되풀이하다 못 했다)
            raise BadRequest("이것은 select 상자가 아니다. 입력 칸이면 type으로 글자를 넣고, 뜬 목록의 option을 click 한다. 단추면 click 해서 목록을 연다")
        loc.select_option(label=option)
        return self._state()

    def scroll(self, args):
        """페이지를 스크롤한다. 페이지가 움직이지 않으면(끝이거나 페이지가 스크롤되지 않는 화면) 화면 안쪽의 스크롤되는 목록을 움직인다 —
        실제 서비스는 목록을 안쪽 영역에 넣는 일이 많다(합성 측정: 기록 목록이 안쪽 영역이라 더 불러오지 못했다)."""
        direction = args.get("direction")
        if direction not in ("up", "down"):
            raise BadRequest("direction: up | down")
        # mouse.wheel은 비동기라 직후 위치가 어긋날 수 있어 scrollBy로 결정적으로 이동한다.
        got = self._js(
            """d => {
              const y0 = window.scrollY; window.scrollBy(0, d);
              const out = {scrolled: 'page', scroll_y: Math.round(window.scrollY), page_height: document.documentElement.scrollHeight, viewport_height: window.innerHeight};
              if (Math.abs(window.scrollY - y0) > 1) return out;
              // 페이지는 움직이지 않았다: 보이는 것 가운데 가장 큰 '안쪽에서 스크롤되는 영역'을 움직인다
              const vis = e => { const r = e.getBoundingClientRect(); return r.width > 50 && r.height > 50 && r.bottom > 0 && r.top < innerHeight; };
              const boxes = Array.from(document.querySelectorAll('*')).filter(e => { const s = getComputedStyle(e);
                return /(auto|scroll)/.test(s.overflowY) && e.scrollHeight > e.clientHeight + 10 && vis(e); });
              boxes.sort((a, b) => b.clientWidth * b.clientHeight - a.clientWidth * a.clientHeight);
              const want = boxes.filter(e => d > 0 ? e.scrollTop + e.clientHeight < e.scrollHeight - 1 : e.scrollTop > 0)[0] || boxes[0];
              if (!want) return out;
              // 한 쪽(영역의 높이)씩 움직인다: 끝에 닿아야 더 불러오는 목록이 많다(0.9쪽씩 움직였더니 끝에 조금 못 미쳐 더 받지 못했다 — 합성 측정)
              const t0 = want.scrollTop, h0 = want.scrollHeight; want.scrollBy(0, d > 0 ? Math.max(200, want.clientHeight) : -Math.max(200, want.clientHeight));
              want.dispatchEvent(new Event('scroll')); want.setAttribute('data-laa-scrolled', '1');
              return {...out, scrolled: 'inner', moved: Math.abs(want.scrollTop - t0) > 1, inner_top: Math.round(want.scrollTop), h0};
            }""",
            600 if direction == "down" else -600,
        )
        if got.get("scrolled") == "inner":
            try:
                self.page.wait_for_timeout(500)   # 스크롤로 더 불러오는 목록: 받아 그릴 틈을 준다
                after = self._js("""() => { const e = document.querySelector('[data-laa-scrolled]'); if (!e) return null; e.removeAttribute('data-laa-scrolled');
                    return {h: e.scrollHeight, end: e.scrollTop + e.clientHeight >= e.scrollHeight - 1}; }""")
            except PlaywrightError:
                after = None
            grew = bool(after) and after["h"] > got.get("h0", 0) + 1
            at_end = bool(after) and after["end"] and not grew   # 끝에 닿았고 더 늘지도 않았다
            return {**self._state(), "scrolled": "inner", "scroll_y": got["scroll_y"], "page_height": got["page_height"],
                    "viewport_height": got["viewport_height"], "at_top": got.get("inner_top", 0) <= 0,
                    "at_bottom": at_end if direction == "down" else False, "grew": grew}
        y, h, vh = got["scroll_y"], got["page_height"], got["viewport_height"]
        return {**self._state(), "scrolled": "page", "scroll_y": y, "page_height": h, "viewport_height": vh,
                "at_top": y <= 0, "at_bottom": y + vh >= h - 1}

    def extract_text(self, args):
        self._login_page()
        ref = _ref(args, required=False)
        if ref:
            text = self._loc(ref).inner_text()
        else:   # 화면 전체: 멈춘 페이지면 한 번 새로 열고 읽는다
            self._wait_thin()
            self._quiet()
            text, _ = self._read(lambda: self.page.locator("body").first.inner_text())  # inner_text는 입력칸 값을 포함하지 않는다
            # 화면 안의 틀(iframe) 글도 붙인다(D121, 실기기 2026-11-18 KOSIS: 통계표가 틀 안에 있어 화면 전체가 15자였다)
            for fr in self.page.frames[1:20]:
                try:
                    inner = fr.evaluate("() => (document.body && document.body.innerText || '').trim()")
                except PlaywrightError:
                    continue
                if isinstance(inner, str) and len(inner) >= 20:
                    text += "\n\n[틀 안] " + inner
        # 빈칸·탭·빈 줄을 줄인다(D121, 실기기 KOSIS: 넓은 표의 칸 사이 공백이 글자 수를 다 써 경기도 줄까지 닿지 못했다)
        text = re.sub(r"[ \t\u00a0]+", " ", text)
        text = re.sub(r" ?\n[ \n]*", "\n", text).strip()
        # max_chars: 공개 페이지 읽기(호스트 코드)가 긴 페이지를 통째로 받아 필요한 줄을 고를 때만 쓴다. 모델의 도구 인자에는 없다
        limit = args.get("max_chars")
        limit = min(limit, 60_000) if isinstance(limit, int) and limit > 0 else MAX_EXTRACT_CHARS
        truncated = len(text) > limit
        return {**self._state(), "text": text[:limit], "truncated": truncated}

    def find_text(self, args):
        """화면 전체의 글에서 낱말을 찾아, 찾은 곳마다 앞뒤 글과 함께 돌려준다(사람의 '페이지에서 찾기').

        긴 화면은 관찰 결과와 extract_text가 앞부분에서 잘린다 — 찾는 값이 그 뒤에 있으면 읽지 못했다(실측 2026-11-15: 릴리스 기록의 옛 버전, 검색 결과의 건수).
        같은 주소의 틀(iframe) 안도 본다. 대소문자는 가리지 않는다. 8곳까지.
        """
        self._login_page()
        q = _str(args, "text", 80)
        if not q.strip():
            raise BadRequest("text: 찾을 낱말")
        self._revive()
        self._quiet()
        got = self._js(
            r"""q => {
              const texts = [document.body ? document.body.innerText : ''];
              for (const f of document.querySelectorAll('iframe')) { try { const d = f.contentDocument; if (d && d.body) texts.push(d.body.innerText); } catch (e) {} }
              const all = texts.join('\n'), low = all.toLowerCase(), key = q.toLowerCase(), out = [];
              let i = 0, n = 0;
              while ((i = low.indexOf(key, i)) !== -1) { n++;
                if (out.length < 8) out.push(all.slice(Math.max(0, i - 100), i + key.length + 140).replace(/\s+/g, ' ').trim());
                i += key.length; }
              return {count: n, hits: out, chars: all.length};
            }""", q.strip())
        return {**self._state(), "count": got["count"], "hits": got["hits"], "page_chars": got["chars"]}

    def links(self, args):
        """화면의 링크(글과 절대 주소). 검색 결과에서 열 페이지를 코드가 고르는 데 쓴다. http/https만, 최대 300개."""
        items = self._js("""() => Array.from(document.querySelectorAll('a[href]')).slice(0, 1500).map(e => ({
            text: (e.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 200), href: e.href || '',
            snippet: ((e.closest('.result, .web-result, li, article') || {}).innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 400)}))""")
        out = [x for x in items or [] if x["text"] and x["href"].startswith(("http://", "https://")) and len(x["href"]) <= 2048][:300]
        return {**self._state(), "links": out}

    def meta(self, args):
        """페이지의 제목과 대표 그림 주소(og:image 등). 링크 카드에 쓴다. 값은 페이지가 적어 둔 것 그대로다(믿을 수 없는 입력)."""
        got = self._js("""() => {
            const m = n => { const e = document.querySelector(`meta[property="${n}"], meta[name="${n}"]`); return e ? (e.content || '').trim() : ''; };
            let img = m('og:image') || m('twitter:image') || m('og:image:url');
            try { if (img) img = new URL(img, location.href).href; } catch (e) { img = ''; }
            return {title: (m('og:title') || document.title || '').trim().slice(0, 200), image: img.slice(0, 2048),
                    site: m('og:site_name').slice(0, 80), description: (m('og:description') || m('description')).slice(0, 300)}; }""")
        return {**self._state(), **(got or {})}

    def site_icon(self, args):
        """지금 페이지의 사이트 아이콘(D110 — 앱의 연동 센터 줄). 페이지가 적어 둔 apple-touch-icon·icon(SVG 빼고), 없으면 /favicon.ico.
        브라우저가 그 페이지를 열 때 받는 것과 같은 그림을 같은 사이트에서 받는다(다른 아이콘 서비스를 거치지 않는다).
        → {"type": 그림 형식, "data": base64} | {"type": None}. 256KB 넘는 것·그림이 아닌 것은 버린다."""
        import base64
        got = self._js("""() => {
            const out = [];
            const rank = r => r.includes('apple-touch-icon') ? 0 : 1;
            [...document.querySelectorAll('link[rel]')].filter(l => /(^|\\s)(apple-touch-icon(-precomposed)?|icon|shortcut icon)(\\s|$)/i.test(l.rel))
              .sort((a, b) => rank(a.rel.toLowerCase()) - rank(b.rel.toLowerCase()))
              .forEach(l => { try { const u = new URL(l.getAttribute('href') || '', location.href);
                  if ((u.protocol === 'https:' || u.protocol === location.protocol && location.protocol === 'http:') && !/\\.svg(\\?|$)/i.test(u.pathname) && !/svg/i.test(l.type || '')) out.push(u.href.slice(0, 2048)); } catch (e) {} });
            try { if (/^https?:$/.test(location.protocol)) out.push(location.origin + '/favicon.ico'); } catch (e) {}
            return [...new Set(out)].slice(0, 4); }""") or []
        for url in got:
            try:
                r = self.context.request.get(url, timeout=8000, max_redirects=3)
            except PlaywrightError:
                continue
            ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            if not r.ok or not (ctype.startswith("image/") or ctype in ("application/octet-stream", "")) or "svg" in ctype:
                continue
            body = r.body()
            if 0 < len(body) <= 256 * 1024:
                return {"type": ctype or "image/x-icon", "data": base64.b64encode(body).decode()}
        return {"type": None}

    def media(self, args):
        """화면의 영상·그림 주소(작업실이 받아 갈 생성 결과 — D96). https이고 커다란 것만, 큰 것부터 30개. 값은 페이지가 준 그대로(믿을 수 없는 입력)."""
        got = self._js("""() => {
            const out = [], seen = new Set();
            const add = (kind, src, w, h) => { try { src = new URL(src, location.href).href; } catch (e) { return; }
                if (!src.startsWith('https://') || seen.has(src)) return; seen.add(src); out.push({kind, src: src.slice(0, 2048), w, h}); };
            document.querySelectorAll('video').forEach(v => { const s = v.currentSrc || v.src || (v.querySelector('source') || {}).src;
                if (s) add('video', s, v.videoWidth || v.clientWidth, v.videoHeight || v.clientHeight); });
            document.querySelectorAll('img').forEach(i => { if ((i.naturalWidth || 0) >= 256 && (i.naturalHeight || 0) >= 256) add('image', i.currentSrc || i.src, i.naturalWidth, i.naturalHeight); });
            return out.sort((a, b) => (b.w * b.h) - (a.w * a.h)).slice(0, 30); }""")
        return {**self._state(), "media": got or []}

    def field_values(self, args):
        """승인 화면용: 입력칸의 실제 값을 자르거나 공백을 합치지 않고 원문 그대로 읽는다 (r-004).

        관찰 결과(observe)의 값은 모델용 요약(200자, 줄바꿈 합침)이라 승인에 쓰면 안 된다.
        비밀번호 칸은 값 대신 가림 표시만 돌려준다. 읽지 못한 ref는 결과에서 빠진다(호스트가 거절 처리).
        """
        refs = args.get("refs")
        if not isinstance(refs, list) or len(refs) > MAX_FIELDS or not all(isinstance(r, str) and REF.match(r) for r in refs):
            raise BadRequest(f"refs: ref 문자열 목록({MAX_FIELDS}개 이하)")
        values, names = {}, {}
        for ref in refs:
            try:
                v = self._loc(ref).evaluate(FIELD_VALUE_JS, timeout=ACTION_TIMEOUT_MS)
            except (PlaywrightError, BadRequest):
                continue
            if isinstance(v, str):
                values[ref] = v[:MAX_FIELD_CHARS]
                try:   # 이름은 보여 주는 데만 쓴다: 못 읽어도 값은 준다
                    n = self._loc(ref).evaluate(FIELD_NAME_JS, timeout=ACTION_TIMEOUT_MS)
                    if isinstance(n, str) and n:
                        names[ref] = n[:40]
                except PlaywrightError:
                    pass
        return {**self._state(), "values": values, "names": names}

    def screenshot(self, args):
        # 사람이 확인하는 용도(모델에게는 주지 않는다). JPEG로 작게.
        # slices=n: 페이지가 창보다 길면 창 높이씩 나눠 n장까지(위에서부터) — 결과 화면이 창 높이에서 잘리던 것(실기기 2026-11-17, 사용자: "여러 장으로 보내 줘도 돼").
        # 기본은 보이는 화면 한 장(승인 직전 화면 등 — 누르려는 곳이 보여야 한다)
        n = args.get("slices")
        if isinstance(n, int) and 2 <= n <= 5:
            try:
                size = self.page.evaluate("() => ({w: document.documentElement.clientWidth || innerWidth, vh: innerHeight, "
                                          "h: Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0, innerHeight)})")
                w, vh, h = int(size["w"]), int(size["vh"]), int(size["h"])
                shots = []
                for k in range(n):
                    y = k * vh
                    if y >= h - 40:   # 남은 것이 거의 없다
                        break
                    data = self.page.screenshot(type="jpeg", quality=65, full_page=True,
                                                clip={"x": 0, "y": y, "width": w, "height": min(vh, h - y)})
                    shots.append(base64.b64encode(data).decode("ascii"))
                if shots:
                    return {**self._state(), "shots": shots, "page_height": h, "viewport_height": vh}
            except PlaywrightError:
                pass   # 나눠 찍지 못했으면 보이는 화면 한 장
        data = self.page.screenshot(type="jpeg", quality=70, full_page=False)
        return {**self._state(), "jpeg_b64": base64.b64encode(data).decode("ascii"), "bytes": len(data)}

    # ---- 손 넘기기 (D106): 사람이 폰에서 이 화면을 보고 직접 누르고 쓴다 — 사람 확인 퍼즐·2단계 인증처럼 사람이 해야 하는 단계 ----
    # 호스트가 손 넘기기를 열어 둔 동안에만 부른다(에이전트는 그동안 아무것도 누르지 않는다). 화면은 어디에도 남기지 않는다.
    HAND_KEYS = ("Enter", "Backspace", "Tab", "Escape", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Delete", "Space")

    def hand_frame(self, args):
        q = args.get("quality", 55)
        q = q if isinstance(q, int) and 30 <= q <= 85 else 55
        data = self.page.screenshot(type="jpeg", quality=q, full_page=False)
        vp = self.page.viewport_size or self.page.evaluate("() => ({width: innerWidth, height: innerHeight})")
        return {"jpeg_b64": base64.b64encode(data).decode("ascii"), "width": vp["width"], "height": vp["height"],
                "url": self.page.url[:300]}

    def hand_input(self, args):
        kind = args.get("kind")
        num = lambda k: float(args[k]) if isinstance(args.get(k), (int, float)) else None
        vp = self.page.viewport_size or {"width": 4000, "height": 4000}
        inside = lambda x, y: x is not None and y is not None and 0 <= x <= vp["width"] and 0 <= y <= vp["height"]
        m, kb = self.page.mouse, self.page.keyboard
        if kind == "tap":
            x, y = num("x"), num("y")
            if not inside(x, y):
                raise BadRequest("x·y: 화면 안의 좌표")
            m.click(x, y)
        elif kind == "drag":
            x, y, x2, y2 = num("x"), num("y"), num("x2"), num("y2")
            if not (inside(x, y) and inside(x2, y2)):
                raise BadRequest("x·y·x2·y2: 화면 안의 좌표")
            m.move(x, y)
            m.down()
            m.move(x2, y2, steps=8)
            m.up()
        elif kind == "wheel":
            dy, dx = num("dy") or 0.0, num("dx") or 0.0
            if abs(dy) > 5000 or abs(dx) > 5000:
                raise BadRequest("dy·dx: ±5000 안")
            x, y = num("x"), num("y")
            if inside(x, y):
                m.move(x, y)
            m.wheel(dx, dy)
        elif kind == "text":
            text = args.get("text")
            if not isinstance(text, str) or not text or len(text) > 500:
                raise BadRequest("text: 1~500자")
            kb.insert_text(text)
        elif kind == "key":
            key = args.get("key")
            if key not in self.HAND_KEYS:
                raise BadRequest("key: " + " | ".join(self.HAND_KEYS))
            kb.press(" " if key == "Space" else key)
        else:
            raise BadRequest("kind: tap | drag | wheel | text | key")
        try:
            self.page.wait_for_timeout(150)
        except PlaywrightError:
            pass
        return {"ok": True}

    # ---- 로그인 전용 (호스트의 로그인 코드만 쓴다) ----
    def secret_type(self, args):
        """사용자가 앱에서 넣은 값(아이디·비밀번호·인증 문자)을 칸에 넣는다. 비밀번호 칸도 된다. 값은 어디에도 남기지 않는다."""
        ref = _ref(args)
        text = args.get("text")
        if not isinstance(text, str) or not text or len(text) > 256:
            raise BadRequest("text: 1~256자 문자열")
        self._loc(ref).fill(text)
        return self._state()

    def element_screenshot(self, args):
        """요소 하나의 그림(QR 코드, 자동입력 방지 문자). 사람에게 보여 주는 용도."""
        data = self._loc(_ref(args)).screenshot(type="png")
        return {**self._state(), "png_b64": base64.b64encode(data).decode("ascii"), "bytes": len(data)}

    def cookie_names(self, args):
        """지금 페이지 주소에 해당하는 쿠키의 **이름만**(값은 주지 않는다). 로그인됐는지 확인하는 데 쓴다."""
        url = _str(args, "url", 2048, required=False) or self.page.url
        return {**self._state(), "names": sorted({c["name"] for c in self.context.cookies(url)})}

    def field_secret(self, args):
        """입력칸의 실제 값(가리지 않음). 사이트가 만들어 보여 준 애플리케이션 비밀번호를 읽어 보안 저장소로 옮길 때만 쓴다(D54)."""
        v = self._loc(_ref(args)).evaluate("e => ('value' in e && typeof e.value === 'string') ? e.value : e.innerText")
        return {**self._state(), "value": v if isinstance(v, str) else ""}

    def dialog_policy(self, args):
        """확인 창을 수락할지(설정 흐름 전용). 마지막으로 뜬 창의 글도 돌려준다.
        keep_popups=true: 누르기가 연 새 창을 그 창 그대로 쓴다(D120 — 로그인한 사이트 안에서 하기)."""
        if "accept" in args:
            self.accept_dialogs = args.get("accept") is True
        if "keep_popups" in args:
            self.keep_popups = args.get("keep_popups") is True
        if "downloads_ok" in args:   # D126: 사용자가 이 사이트에서 내려받기를 허락했다
            self.downloads_ok = args.get("downloads_ok") is True
        if "late_popups" in args:   # 누르고 0.3초 뒤에야 뜬 새 창도 다음 동작 때 받아 온다(D120, 실기기 2026-11-18 서울교통공사 '열차시각표')
            self.late_popups = args.get("late_popups") is True
        last, self.last_dialog = self.last_dialog, None
        return {**self._state(), "last_dialog": last}

    def cookies_export(self, args):
        """로그인된 상태를 호스트의 암호화 저장소(키체인)에 넣기 위해 넘긴다(D52): 쿠키(세션 쿠키 포함)와, 페이지가 브라우저 저장소에 둔 것.

        쿠키만으로는 안 되는 사이트가 있다 — 로그인 표식을 localStorage·sessionStorage에 두는 서비스(실기기: 로그인은 됐는데 다음에 열자 풀려 있었다).
        origins: [{origin, localStorage: [{name, value}]}], session: 지금 화면의 sessionStorage.
        이 컨테이너에는 남기지 않는다(로그인용 브라우저는 일회용 프로필로 뜬다). 호스트는 값을 기록하지 않는다.
        """
        state = self.context.storage_state()
        origins = []
        for o in state.get("origins", []):
            items = [{"name": str(i.get("name", ""))[:200], "value": i["value"]} for i in o.get("localStorage", [])
                     if isinstance(i.get("value"), str) and len(i["value"]) <= STORAGE_VALUE_MAX]
            if items:
                origins.append({"origin": o.get("origin", ""), "localStorage": items[:STORAGE_ITEMS_MAX]})
        session = None
        if self._awake(1500):
            try:
                got = self._js("() => ({origin: location.origin, items: Object.entries(sessionStorage)})") or {}
                items = [[str(k)[:200], v] for k, v in got.get("items", []) if isinstance(v, str) and len(v) <= STORAGE_VALUE_MAX and k != RESTORED_MARK]
                if items:
                    session = {"origin": got.get("origin", ""), "items": items[:STORAGE_ITEMS_MAX]}
            except PlaywrightError:
                pass
        return {**self._state(), "cookies": state.get("cookies", []), "origins": origins, "session": session}

    def cookies_import(self, args):
        """보관해 둔 로그인 상태를 되살린다. 페이지를 열기 전에 부른다. 브라우저 저장소의 값은 그 주소의 화면이 열릴 때 넣는다(이미 있는 값은 건드리지 않는다)."""
        cookies = args.get("cookies")
        if not isinstance(cookies, list) or len(cookies) > 500 or not all(isinstance(c, dict) for c in cookies):
            raise BadRequest("cookies: 쿠키 객체 목록(500개 이하)")
        self.context.add_cookies(cookies)
        local, sess = {}, {}
        for o in args.get("origins") or []:
            if isinstance(o, dict) and isinstance(o.get("origin"), str) and isinstance(o.get("localStorage"), list):
                local[o["origin"]] = {str(i["name"]): i["value"] for i in o["localStorage"][:STORAGE_ITEMS_MAX]
                                      if isinstance(i, dict) and isinstance(i.get("value"), str) and len(i["value"]) <= STORAGE_VALUE_MAX}
        se = args.get("session")
        if isinstance(se, dict) and isinstance(se.get("origin"), str) and isinstance(se.get("items"), list):
            sess[se["origin"]] = {str(k): v for k, v in (x for x in se["items"][:STORAGE_ITEMS_MAX] if isinstance(x, list) and len(x) == 2)
                                  if isinstance(v, str) and len(v) <= STORAGE_VALUE_MAX}
        if local or sess:
            # 탭마다 한 번만 넣는다(표식): 사이트가 그 뒤에 지운 값(로그아웃)을 다음 화면에서 되살리지 않게
            self.context.add_init_script(
                "(() => { try { const L = %s, S = %s, o = location.origin; if (sessionStorage.getItem(%s)) return; "
                "for (const [k, v] of Object.entries(L[o] || {})) if (localStorage.getItem(k) === null) localStorage.setItem(k, v); "
                "for (const [k, v] of Object.entries(S[o] || {})) if (sessionStorage.getItem(k) === null) sessionStorage.setItem(k, v); "
                "if (L[o] || S[o]) sessionStorage.setItem(%s, '1'); } catch (e) {} })();"
                % (json.dumps(local, ensure_ascii=False), json.dumps(sess, ensure_ascii=False), json.dumps(RESTORED_MARK), json.dumps(RESTORED_MARK)))
        return {**self._state(), "count": len(cookies), "origins": len(local), "session": bool(sess)}

    # ---- 공통 로그인(D70): 칸을 표준 속성으로 알아보고, 사용자가 앱에서 넣은 값을 그 칸에 넣고, 제출한다. 값은 어디에도 남기지 않는다 ----
    def _login_form(self):
        """로그인 칸이 있는 프레임과 그 꼴. 본문에 없으면 안쪽 프레임(같은 페이지의 iframe)에서 찾는다."""
        best = None
        for fr in [self.page.main_frame, *[f for f in self.page.frames if f is not self.page.main_frame]][:8]:
            try:   # evaluate에는 시간 한도가 없다 → 한도가 있는 wait_for_function으로 묻는다(돌려주는 객체는 언제나 참이다)
                got = self._js(LOGIN_FORM_JS, frame=fr)
            except PlaywrightError:
                continue
            if got.get("fields") and (best is None or (got["passwords"] and not best[1]["passwords"])):
                best = (fr, got)
            if best is not None and best[1]["passwords"]:
                break
        return best or (self.page.main_frame, {"fields": [], "submit": -1, "image": -1, "signup": False, "passwords": 0, "inForm": False})

    def form_fields(self, args):
        """지금 화면의 로그인 칸: [{name(id|password|code|captcha), kind(text|password|code|captcha), label}]. 값은 읽지 않는다."""
        self._login_page()
        self._revive()
        fr, got = self._login_form()
        url = urlsplit(fr.url if got["fields"] else self.page.url)
        return {**self._state(), "host": url.hostname or "", "https": url.scheme == "https",
                "fields": [{"name": f["role"], "kind": f["kind"], "label": f["label"]} for f in got["fields"]],
                "submit": got["submit"] >= 0, "captcha_image": got["image"] >= 0, "signup": bool(got["signup"]),
                # 제출 뒤를 가르는 꼴(참·거짓뿐): 비밀번호 칸이 비었나, 오류 안내, 덮는 층, 확인 절차(캡차), 제출 단추가 꺼졌나
                "pw_empty": got.get("pwEmpty"), "alert": bool(got.get("alert")), "alert_text": got.get("alertText") or "", "dialog": bool(got.get("dialog")),
                "challenge": bool(got.get("challenge")), "submit_off": got.get("submitOff")}

    def form_fill(self, args):
        """사용자가 앱에서 넣은 값을 칸에 넣는다. values: {칸 이름: 값}. 화면의 칸이 그 사이 바뀌었으면 넣지 않는다."""
        values = args.get("values")
        if not isinstance(values, dict) or not values or \
                any(not isinstance(k, str) or not isinstance(v, str) or not v or len(v) > 256 for k, v in values.items()):
            raise BadRequest("values: {칸 이름: 1~256자 문자열}")
        self._login_page()
        fr, got = self._login_form()
        if sorted(f["role"] for f in got["fields"]) != sorted(values):
            raise BadRequest("화면의 입력 칸이 바뀌었다 (form_fields를 다시 한다)")
        for f in got["fields"]:
            fr.locator("input").nth(f["i"]).fill(values[f["role"]])
        return {**self._state(), "filled": len(got["fields"])}

    def form_submit(self, args):
        """로그인 칸을 제출한다: 양식의 제출 단추(없으면 칸 다음의 로그인·다음·확인 단추, 그것도 없으면 마지막 칸에서 Enter)."""
        self._login_page()
        fr, got = self._login_form()
        if not got["fields"]:
            raise BadRequest("제출할 로그인 칸이 없다")
        last = fr.locator("input").nth(got["fields"][-1]["i"])
        btn = fr.locator(LOGIN_BUTTONS).nth(got["submit"]) if got["submit"] >= 0 else None
        # 제출 단추가 있으면 그 단추를 누른다 — 사람이 하는 방식이고, 단추의 누르기에 붙은 처리를 거쳐야 받는 로그인 화면이 있다
        # (실기기: 같은 계정이 단추로는 통과했고, 마지막 칸의 Enter로 바꾼 뒤에는 사이트가 매번 거절했다).
        # 단추가 없거나 누르지 못할 때만 Enter. 한 방법이 됐으면 다른 방법을 또 쓰지 않는다(두 번 제출하지 않는다).
        how = None
        if btn is not None:
            try:
                btn.click(timeout=4000)
                how = "button"
            except PlaywrightError:
                try:
                    btn.scroll_into_view_if_needed(timeout=1500)
                    btn.click(timeout=2500)
                    how = "button"
                except PlaywrightError:
                    how = None
        if how is None:
            last.press("Enter", timeout=3000)
            how = "enter"
        try:
            self.page.wait_for_timeout(400)   # 제출 뒤 팝업이 스스로 닫히는 로그인: 닫혔으면 원래 탭으로
        except PlaywrightError:
            pass
        self._login_page()
        self._settle()
        return {**self._state(), "how": how}

    # ---- 간편인증 입력 화면(D79): 꼴을 읽고, 사용자가 앱에서 넣은 값과 고른 것을 그대로 넣는다. 값은 어디에도 남기지 않는다 ----
    def _easy(self):
        self._login_page()
        for fr in [self.page.main_frame, *[f for f in self.page.frames if f is not self.page.main_frame]][:8]:
            try:
                got = self._js(EASY_FORM_JS, frame=fr)
            except PlaywrightError:
                continue
            roles = {f["role"] for f in got.get("fields", [])}
            if {"birth", "phone"} <= roles and got.get("submit"):
                return fr, got
        return self.page.main_frame, None

    def _qr(self):
        self._login_page()
        self._revive()
        try:
            return self._js(QR_FORM_JS)
        except PlaywrightError:
            return {"tab": None, "shown": False, "around": "", "number": None, "expired": False, "refresh": None}

    def qr_form(self, args):
        """QR 로그인: tab(그 방법으로 가는 탭·단추의 글, 없으면 None), shown(QR 그림이 떠 있다), around(그림 둘레의 글),
        number(둘레의 글에 숫자가 하나뿐일 때 그 숫자), expired, refresh(다시 받기 단추의 글)."""
        got = self._qr()
        url = urlsplit(self.page.url)
        return {**self._state(), "host": url.hostname or "", "https": url.scheme == "https", **got}

    def qr_press(self, args):
        """what: "tab"(QR 로그인 탭을 누른다) | "refresh"(QR 다시 받기). 화면에 그것이 없으면 오류."""
        what = _str(args, "what", 10)
        if what not in ("tab", "refresh"):
            raise BadRequest("what: tab | refresh")
        got = self._qr()
        if not got.get("tab" if what == "tab" else "refresh"):
            raise BadRequest("화면에 그 단추가 없다")
        self.page.locator(f'[data-laa-qr="{what}"]').first.click(timeout=5000)
        return self._state()

    def qr_image(self, args):
        """QR 그림(사람에게 보여 준다 — 폰으로 찍는다). 없으면 오류."""
        if not self._qr().get("shown"):
            raise BadRequest("QR 그림이 없다")
        data = self.page.locator('[data-laa-qr="img"]').first.screenshot(type="png")
        return {**self._state(), "png_b64": base64.b64encode(data).decode("ascii"), "bytes": len(data)}

    def easy_form(self, args):
        """간편인증 입력 화면인가, 그 꼴: providers(인증 기관 이름), fields(name|birth|phone), prefix(전화 앞자리 고르기), consents(동의 항목).
        rrn: 주민등록번호 칸이 있다(그런 화면에는 넣지 않는다). other: 알아보지 못한 다른 칸의 수."""
        self._revive()
        fr, got = self._easy()
        if got is None:
            return {**self._state(), "easy": False}
        url = urlsplit(fr.url or self.page.url)
        return {**self._state(), "easy": True, "host": url.hostname or "", "https": url.scheme == "https",
                "providers": [p["label"] for p in got["providers"]], "fields": [{"name": f["role"], "label": f["label"]} for f in got["fields"]],
                "prefix": got["prefix"], "consents": got["consents"], "rrn": bool(got["rrn"]), "other": int(got["other"])}

    def easy_fill(self, args):
        """provider: 고른 인증 기관(easy_form이 준 이름 그대로), values: {name, birth, phone}, consents: 사용자가 동의한 항목의 이름들.
        화면이 그 사이 바뀌었으면 넣지 않는다. 주민등록번호 칸이 있는 화면에는 넣지 않는다. 누르지는 않는다("인증 요청"은 login_press)."""
        values, provider, consents = args.get("values"), args.get("provider"), args.get("consents") or []
        if not isinstance(values, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not v or len(v) > 64 for k, v in values.items()):
            raise BadRequest("values: {칸 이름: 1~64자 문자열}")
        if not isinstance(consents, list) or any(not isinstance(c, str) for c in consents):
            raise BadRequest("consents: 동의 항목 이름 목록")
        fr, got = self._easy()
        if got is None or got["rrn"]:
            raise BadRequest("간편인증 입력 화면이 아니다")
        if sorted(f["role"] for f in got["fields"]) != sorted(values):
            raise BadRequest("화면의 입력 칸이 바뀌었다 (easy_form을 다시 한다)")
        labels = [p["label"] for p in got["providers"]]
        if labels:
            if provider not in labels:
                raise BadRequest("화면에 그 인증 기관이 없다")
            fr.locator(f'[data-laa-easy="p-{labels.index(provider)}"]').click(timeout=4000)
        known = [c["label"] for c in got["consents"]]
        if any(c not in known for c in consents):
            raise BadRequest("화면에 그 동의 항목이 없다")
        phone = "".join(ch for ch in values.get("phone", "") if ch.isdigit())
        for f in got["fields"]:
            v = values[f["role"]]
            if f["role"] == "phone":
                if got["prefix"]:
                    if phone[:3] not in got["prefix"]:
                        raise BadRequest("전화번호 앞자리를 화면에서 고를 수 없다")
                    fr.locator('[data-laa-easy="prefix"]').select_option(phone[:3], timeout=3000)
                    v = phone[3:]
                else:
                    v = phone
            elif f["role"] == "birth":
                v = "".join(ch for ch in v if ch.isdigit())
            fr.locator(f'[data-laa-easy="f-{f["role"]}"]').fill(v, timeout=4000)
        checked = 0
        for c in consents:
            i = known.index(c)
            box = fr.locator(f'[data-laa-easy="c-{i}"]')
            if not box.is_checked():
                try:
                    box.check(timeout=1500)
                except PlaywrightError:   # 꾸민 체크 칸: 보이는 이름표를 누른다
                    fr.locator(f'[data-laa-easy="cl-{i}"]').first.click(timeout=3000)
            if not box.is_checked():
                raise BadRequest("동의 항목에 표시하지 못했다")
            checked += 1
        return {**self._state(), "filled": len(got["fields"]), "checked": checked}

    def form_image(self, args):
        """캡차 그림(사람에게 보여 준다). 없으면 오류."""
        fr, got = self._login_form()
        if got["image"] < 0:
            raise BadRequest("캡차 그림이 없다")
        data = fr.locator("img, canvas").nth(got["image"]).screenshot(type="png")
        return {**self._state(), "png_b64": base64.b64encode(data).decode("ascii"), "bytes": len(data)}

    def _login_page(self):
        """로그인 창(팝업)이 닫혔으면 원래 탭으로 돌아온다. 소셜 로그인은 팝업에서 하고 끝나면 스스로 닫는 곳이 있다."""
        opener = getattr(self, "_opener", None)
        if opener is not None and self.page.is_closed():
            self.page, self._opener = opener, None
            self.refmap = {}

    def login_opener(self, args):
        """로그인 창(팝업)에서 이어 가는 동안 원래 탭의 주소(없으면 None). close=true면 팝업을 닫고 원래 탭으로 돌아온다.
        간편인증 창이 '확인 중'인 채 남아 있는데 원래 탭은 이미 사이트로 넘어간 때가 있다(실기기 2026-11-17 브이월드) — 원래 탭이 움직였는지 보는 데 쓴다."""
        opener = getattr(self, "_opener", None)
        if opener is None or opener.is_closed():
            return {**self._state(), "opener": None}
        url = opener.url
        if args.get("close") is True:
            try:
                self.page.close()
            except PlaywrightError:
                pass
            self.page, self._opener = opener, None
            self.refmap = {}
        return {**self._state(), "opener": url[:2048]}

    def login_methods(self, args):
        """지금 화면의 로그인 방법: providers(kakao|naver|google|apple 가운데 단추가 있는 것), entry(로그인 화면으로 가는 "로그인" 입구가 있는가)."""
        self._login_page()
        self._revive()
        try:
            got = self._js(LOGIN_METHODS_JS)
        except PlaywrightError:
            got = {"providers": [], "entry": -1}
        url = urlsplit(self.page.url)
        return {**self._state(), "host": url.hostname or "", "providers": [x["who"] for x in got["providers"]], "entry": got["entry"] >= 0 or got["entry"] == -2,
                "entry_kind": got.get("entryKind"), "provider_logins": [x["who"] for x in got["providers"] if x.get("login")]}

    def login_open(self, args):
        """로그인 방법을 고른다: {"provider": "kakao"}는 그 소셜 로그인 단추를, {"entry": true}는 "로그인" 입구를 누른다.

        새 창(팝업)이 열리면 그 창에서 이어 간다(원래 탭은 그대로 둔다 — 팝업이 끝나고 원래 탭에 알려 주는 로그인이 있다).
        """
        self._login_page()
        want = args.get("provider")

        def find() -> int:
            try:
                got = self._js(LOGIN_METHODS_JS)
            except PlaywrightError:
                raise BadRequest("로그인 화면을 읽지 못했다") from None
            if isinstance(args.get("card"), str):   # 이름 없는 방법 카드: login_cards가 준 이름 그대로
                try:
                    cards = self._js(LOGIN_CARDS_JS)["cards"]
                except PlaywrightError:
                    raise BadRequest("로그인 화면을 읽지 못했다") from None
                return next((x["i"] for x in cards if x["label"] == args["card"]), -1)
            return got["entry"] if args.get("entry") is True else next((x["i"] for x in got["providers"] if x["who"] == want), -1)

        self._revive()
        i = find()
        if i < 0:   # 화면이 그려지는 도중이었을 수 있다: 잠깐 뒤 한 번 더 본다
            self.page.wait_for_timeout(1200)
            i = find()
        if i == -2 and args.get("entry") is True:   # 단추가 아닌 글 입구(표시를 붙여 둔 것)
            self._popup = None
            try:
                self.page.locator("[data-laa-entry]").first.click(timeout=5000)
            except PlaywrightError:
                raise BadRequest("화면에 그 로그인 방법이 없다") from None
            i = None
        elif i < 0:
            raise BadRequest("화면에 그 로그인 방법이 없다")
        self._popup = None if i is not None else self._popup
        try:
            if i is not None:
                self.page.locator(LOGIN_BUTTONS).nth(i).click(timeout=5000)
        except PlaywrightError:
            # 누르는 도중에 페이지가 멈췄거나(실제 사이트: 처음 받을 때 멈춤) 단추가 가려졌다: 살려서 다시 찾아 누른다(그래도 안 되면 요소에 직접)
            self._revive()
            i = find()
            if i < 0:
                raise BadRequest("화면에 그 로그인 방법이 없다") from None
            loc = self.page.locator(LOGIN_BUTTONS).nth(i)
            try:
                loc.scroll_into_view_if_needed(timeout=2000)
                loc.click(timeout=4000)
            except PlaywrightError:
                loc.evaluate("e => e.click()")
        try:
            self.page.wait_for_timeout(600)
        except PlaywrightError:
            pass
        pop, self._popup = self._popup, None
        how = "same_tab"
        if pop is not None and not pop.is_closed():
            try:
                pop.wait_for_load_state("domcontentloaded", timeout=6000)
            except PlaywrightError:
                pass
            pop.on("dialog", self._on_dialog)
            self._opener, self.page = self.page, pop
            how = "popup"
        self.refmap = {}
        self._revived = False
        self._settle()
        return {**self._state(), "how": how}

    def login_cards(self, args):
        """로그인 화면의 방법 카드 이름들(누르는 것에 글이 없고 옆의 머리글에 이름이 있는 화면). 누르는 것은 login_open {"card": 이름}."""
        self._login_page()
        self._revive()
        try:
            got = self._js(LOGIN_CARDS_JS)
        except PlaywrightError:
            got = {"cards": []}
        url = urlsplit(self.page.url)
        return {**self._state(), "host": url.hostname or "", "cards": [x["label"] for x in got["cards"]]}

    def login_buttons(self, args):
        """칸이 없는 중간 화면에서 누를 수 있는 단추의 글(최대 5개). 예: "계속하기", "다른 계정으로 로그인"."""
        self._login_page()
        self._revive()
        try:
            got = self._js(LOGIN_ACTIONS_JS)
        except PlaywrightError:
            got = {"buttons": []}
        url = urlsplit(self.page.url)
        return {**self._state(), "host": url.hostname or "", "buttons": [x["t"] for x in got["buttons"]]}

    def login_press(self, args):
        """사용자가 고른 단추를 누른다. text: login_buttons가 준 글 그대로(다른 글은 받지 않는다)."""
        self._login_page()
        text = _str(args, "text", 40)
        try:
            got = self._js(LOGIN_ACTIONS_JS)
        except PlaywrightError:
            raise BadRequest("화면을 읽지 못했다") from None
        i = next((x["i"] for x in got["buttons"] if x["t"] == text), -1)
        if i < 0:
            raise BadRequest("화면에 그 단추가 없다 (login_buttons를 다시 한다)")
        self._popup = None
        self.page.locator(LOGIN_BUTTONS).nth(i).click(timeout=5000)
        try:
            self.page.wait_for_timeout(600)
        except PlaywrightError:
            pass
        self._popup = None
        self._login_page()
        self.refmap = {}
        self._settle()
        return self._state()

    def _challenge(self):
        """확인 문제 상자의 꼴과, 답 칸이 있는 곳(본문 또는 상자 안의 틀). → (got, 답 칸이 있는 프레임 또는 None, 틀의 꼴)"""
        try:
            got = self._js(CHALLENGE_JS)
        except PlaywrightError:
            return {"present": False}, None, {}
        if not got.get("present"):
            return got, None, {}
        if got.get("inputs") == 1:
            return got, self.page.main_frame, {}
        inner = {}
        target = None
        for fr in self.page.frames:   # 상자 안에 든 틀: 문제를 다른 주소의 틀에 넣는 사이트가 있다(실기기 — 상자의 글은 "…답해 주세요. 닫기"뿐이었다)
            if fr is self.page.main_frame:
                continue
            try:
                el = fr.frame_element()
                if not el.evaluate("e => !!e.closest('[data-laa-ch=box]')"):
                    continue
                shape = self._js(CHALLENGE_FRAME_JS, frame=fr)
            except PlaywrightError:
                continue
            host = urlsplit(fr.url).hostname or ""
            if not inner or shape.get("inputs") == 1:
                inner = {**shape, "host": host}
            if shape.get("inputs") == 1:
                target = fr
                break
        return got, target, inner

    def challenge(self, args):
        """로그인 화면 위에 뜬 확인 문제(보안문자)를 사람에게 보여 줄 수 있게 읽는다: 문제의 글, 답을 쓸 칸이 하나 있는가, 그 상자의 그림.
        풀지 않는다 — 사용자가 앱에서 보고 답한다(네이버 로그인의 그림 문제와 같다). 그림은 답할 수 없는 종류여도 준다(무슨 문제인지 사용자가 볼 수 있게)."""
        self._login_page()
        self._revive()
        got, target, inner = self._challenge()
        text = " ".join(x for x in (got.get("text", ""), inner.get("text", "")) if x)[:400]
        out = {**self._state(), "present": bool(got.get("present")), "text": text, "answerable": target is not None,
               "label": (inner.get("label") if target is not None and target is not self.page.main_frame else got.get("label", "")) or "",
               "frame": bool(got.get("frame")),
               # 기록용 꼴(개수·호스트뿐): 어떤 종류의 문제인지 가르는 데 쓴다
               "shape": {"box_inputs": got.get("inputs", 0), "box_pictures": got.get("pictures", 0), "frame_host": inner.get("host", ""),
                         "frame_inputs": inner.get("inputs", -1), "frame_buttons": inner.get("buttons", -1),
                         "frame_pictures": inner.get("pictures", -1), "frame_clickables": inner.get("clickables", -1)}}
        if out["present"]:
            try:
                shot = self.page.locator("[data-laa-ch=box]").first
                if target is not None and target is not self.page.main_frame:
                    shot = target.frame_element()   # 문제가 든 틀만: 바깥 상자는 배경까지 찍혀 문제가 작게 보인다(실기기)
                data = shot.screenshot(type="png")
                if len(data) > 500_000:   # 너무 크면 화질을 낮춘다
                    data = shot.screenshot(type="jpeg", quality=60)
                    out["mime"] = "image/jpeg"
                else:
                    out["mime"] = "image/png"
                out["b64"], out["bytes"] = base64.b64encode(data).decode("ascii"), len(data)
            except PlaywrightError:
                pass
        return out

    def challenge_answer(self, args):
        """사용자가 쓴 답을 확인 문제의 칸에 넣고 그 상자(또는 틀)의 확인 단추를 누른다(없으면 Enter). 답은 남기지 않는다."""
        self._login_page()
        text = _str(args, "text", 100)
        got, target, inner = self._challenge()
        if not got.get("present") or target is None:
            raise BadRequest("답을 쓸 칸이 하나인 확인 문제가 아니다")
        box = target.locator("[data-laa-ch=input]").first
        box.fill(text)
        how = "enter"
        has_ok = inner.get("ok") if target is not self.page.main_frame else got.get("ok")
        for _ in range(4):
            # 답을 쓰기 전에는 내는 단추가 꺼져 있는 문제가 있다(실기기: 단추를 못 찾아 Enter로 냈고 문제가 한 번 더 나왔다) → 답을 넣은 뒤에 다시 찾는다
            if has_ok:
                break
            self.page.wait_for_timeout(300)
            got2, target2, inner2 = self._challenge()
            if target2 is None:
                break
            target = target2
            has_ok = inner2.get("ok") if target is not self.page.main_frame else got2.get("ok")
        box = target.locator("[data-laa-ch=input]").first
        if has_ok:
            try:
                target.locator("[data-laa-ch=ok]").first.click(timeout=4000)
                how = "button"
            except PlaywrightError:
                box.press("Enter", timeout=3000)
        else:
            box.press("Enter", timeout=3000)
        self._settle()
        return {**self._state(), "how": how}

    def close(self):
        self.context.close()  # 영구 프로필이면 여기서 쿠키가 디스크에 기록된다
        if self.browser is not None:
            self.browser.close()


LATE_POPUP_OPS = ("observe", "extract_text", "find_text", "links", "screenshot")   # 읽기만(누르기 전에 바꾸면 고른 ref가 엉뚱한 화면의 것이 된다)
OPS = ("download_table", "table_query", "hand_frame", "hand_input", "navigate", "observe", "click", "type", "select_option", "scroll", "press_key", "hover", "drag", "find_text", "extract_text", "field_values", "screenshot",
       "secret_type", "element_screenshot", "cookie_names", "cookies_export", "cookies_import",
       "field_secret", "dialog_policy", "links", "meta", "site_icon", "media", "form_fields", "form_fill", "form_submit", "form_image", "easy_form", "easy_fill", "qr_form", "qr_press", "qr_image", "login_methods", "login_opener", "login_open", "login_cards", "login_buttons", "login_press", "challenge", "challenge_answer")


def main() -> int:
    with sync_playwright() as pw:
        b = Browser(pw)
        print(json.dumps({"id": 0, "ok": True, "result": {"ready": True}}), flush=True)
        for line in sys.stdin.buffer:
            if len(line) > MAX_REQUEST_BYTES:
                reply = {"id": None, "ok": False, "error": "요청이 너무 크다"}
                print(json.dumps(reply), flush=True)
                continue
            try:
                req = json.loads(line)
                rid, op, args = req.get("id"), req.get("op"), req.get("args") or {}
                if not isinstance(args, dict):
                    raise BadRequest("args는 객체여야 한다")
            except (json.JSONDecodeError, AttributeError, BadRequest) as e:
                print(json.dumps({"id": None, "ok": False, "error": f"잘못된 요청: {e}"}), flush=True)
                continue
            if op == "close":
                print(json.dumps({"id": rid, "ok": True, "result": {}}), flush=True)
                break
            if op not in OPS:
                reply = {"id": rid, "ok": False, "error": f"허용되지 않은 op: {op}"}
            else:
                try:
                    if getattr(b, "keep_popups", False):
                        b._login_page()   # 그대로 쓰던 새 창(사이트 안의 새 창 D120)이 닫혔으면 원래 탭으로
                    if getattr(b, "late_popups", False) and op in LATE_POPUP_OPS and getattr(b, "_popup", None) is not None:
                        b._adopt_popup()   # 지난 누르기가 늦게 연 새 창: 지금 받아 온다(그대로 쓰거나 이 탭에서 연다)
                    reply = {"id": rid, "ok": True, "result": getattr(b, op)(args)}
                except BadRequest as e:
                    reply = {"id": rid, "ok": False, "error": f"잘못된 인자: {e}"}
                except PlaywrightError as e:
                    reply = {"id": rid, "ok": False, "error": str(e).splitlines()[0][:300]}
                except Exception as e:   # 예상하지 못한 오류로 서비스가 통째로 죽지 않게 한다(실제 사이트에서 관찰 중 죽은 일이 있었다)
                    reply = {"id": rid, "ok": False, "error": f"{type(e).__name__}: {str(e).splitlines()[0][:200] if str(e) else ''}"}
            print(json.dumps(reply, ensure_ascii=False), flush=True)
        b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
