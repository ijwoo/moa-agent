"""합성 플래너(login.test/board): 실제 서비스에서 보는 복잡한 화면 꼴을 흉내 낸다 (D72 — 로그인한 사이트 안에서 하기 측정용).

지금까지의 합성 서비스(/home)는 링크와 단순한 양식뿐이라 22/22가 나왔다. 실제 서비스는 스크립트로 화면을 그리고,
이름표 없는 칸·팝업 메뉴·달력·창 위의 입력 창·"더 보기"·끌어 옮기기를 쓴다. 그 꼴을 여기에 모았다. 모두 합성 데이터.

  화면: GET /board (로그인 필요)
  상태: GET /board/state → JSON, 바꾸기: POST /board/{project,add,update,toggle,delete} (양식 값)
"""

from __future__ import annotations

import copy
import json

_TITLES = ["주간 회의 준비", "합성 보고서 초안", "디자인 시안 확인", "낡은 메모 정리", "거래처 견적 회신", "예산 초안 작성", "서버 점검 일정 잡기",
           "신규 입사자 안내", "분기 목표 정리", "홍보 문구 검토", "출장 경비 정산", "보안 교육 수강", "회의실 예약 확인", "발표 자료 다듬기",
           "고객 문의 답변", "재고 목록 갱신", "계약서 초안 검토", "뉴스레터 발송 준비", "사내 설문 정리", "예산 집행 내역 확인", "교육 일정 공지",
           "장비 반납 처리", "회고 메모 작성", "협력사 미팅 준비", "문서 양식 통일", "명함 주문", "월간 지표 정리", "공용 폴더 정리",
           "행사 장소 답사", "연말 인사 메일 초안"]
_DUES = ["2026-11-18", "2026-11-19", "", "2026-11-16", "2026-11-24", "2026-12-01", "", "2026-11-17", "2026-11-30", "",
         "2026-11-21", "2026-11-25", "", "2026-11-20", "2026-11-23", "", "2026-12-04", "2026-11-26", "", "2026-11-20",
         "2026-12-02", "", "2026-11-28", "2026-12-08", "", "2026-11-22", "2026-12-10", "", "2026-12-12", "2026-12-15"]
_STATUS = ["todo", "doing", "todo", "todo", "done", "todo", "doing", "done", "todo", "todo",
           "done", "todo", "todo", "doing", "todo", "done", "todo", "todo", "done", "todo",
           "todo", "done", "todo", "todo", "doing", "todo", "todo", "done", "todo", "todo"]
_PRIO = ["보통", "높음", "보통", "낮음", "보통", "높음", "보통", "낮음", "보통", "보통"] * 3

PEOPLE = ["한지우", "한서준", "박도윤", "이하린", "최민재", "정유나"]
_ACTS = ["할 일 3건 완료", "새 프로젝트 논의", "거래처 미팅 기록", "예산 초안 공유", "주간 회의록 올림", "디자인 시안 1차 수정", "서버 점검 완료",
         "신규 입사자 자리 배정", "분기 목표 초안 작성", "홍보 문구 2차 검토"]
# 기록: 11월 15일부터 하루씩 거슬러 60개(화면은 15개씩 — 안쪽 목록을 스크롤해야 더 받는다). 10월 20일의 기록은 "색상표 초안 공유"(다른 날에는 없는 글)
ACTIVITY = []
for _i in range(60):
    _m, _d = (11, 15 - _i) if _i < 15 else (10, 31 - (_i - 15)) if _i < 46 else (9, 30 - (_i - 46))
    ACTIVITY.append({"date": f"{_m}월 {_d}일", "text": "색상표 초안 공유" if (_m, _d) == (10, 20) else _ACTS[_i % len(_ACTS)]})

BOARD_INITIAL = {
    "project": "기본",
    "next_id": 100,
    "sort": "none",       # none | due | prio — 정렬(select)
    "note": "",           # 공용 메모장(틀 안의 화면)
    "archived": 0,        # 보관한 완료 할 일 수(브라우저 확인 창을 거친다)
    "tasks": {
        "기본": [{"id": i + 1, "title": t, "due": _DUES[i], "prio": _PRIO[i], "status": _STATUS[i], "memo": "", "star": False, "assignee": "", "tags": ""}
               for i, t in enumerate(_TITLES)],
        "가상 리뉴얼": [{"id": 51, "title": "첫 화면 문구 정하기", "due": "2026-11-19", "prio": "높음", "status": "doing", "memo": "", "star": False, "assignee": "", "tags": ""},
                    {"id": 52, "title": "색상표 확정", "due": "", "prio": "보통", "status": "todo", "memo": "", "star": False, "assignee": "", "tags": ""},
                    {"id": 53, "title": "사진 고르기", "due": "2026-11-27", "prio": "보통", "status": "todo", "memo": "", "star": False, "assignee": "", "tags": ""},
                    {"id": 54, "title": "옛 화면 내리기", "due": "2026-12-05", "prio": "낮음", "status": "done", "memo": "", "star": False, "assignee": "", "tags": ""}],
    },
    "log": [],   # 무엇을 바꿨는지(종류와 id) — 측정의 판정용
}


def initial() -> dict:
    return copy.deepcopy(BOARD_INITIAL)


def api(method: str, path: str, form: dict, st: dict) -> str | None:
    """상태를 읽거나 바꾼다 → JSON 글(모르는 경로면 None)."""
    one = lambda k: (form.get(k) or [""])[0]
    tasks = st["tasks"][st["project"]]
    find = lambda: next((t for t in tasks if str(t["id"]) == one("id")), None)
    if path == "/board/state":
        return json.dumps({"project": st["project"], "projects": list(st["tasks"]), "tasks": tasks, "sort": st["sort"], "people": PEOPLE}, ensure_ascii=False)
    if path == "/board/activity":   # 기록: 15개씩(스크롤하면 더 받는다)
        off = int(one("offset")) if one("offset").isdigit() else 0
        return json.dumps({"items": ACTIVITY[off:off + 15], "total": len(ACTIVITY)}, ensure_ascii=False)
    if path == "/board/note" and method == "GET":   # 틀 안에 들어가는 메모장 화면
        from html import escape
        return ("<!doctype html><html lang='ko'><head><meta charset='utf-8'><title>공용 메모장</title></head><body style='font:15px sans-serif'>"
                f"<form method='post' action='/board/note'><textarea name='note' rows='5' cols='40' placeholder='메모를 적어 주세요'>{escape(st['note'])}</textarea>"
                "<br><button type='submit'>저장</button></form></body></html>")
    if method != "POST":
        return None
    if path == "/board/project":
        if one("name") in st["tasks"]:
            st["project"] = one("name")
            st["log"].append(["project", one("name")])
    elif path == "/board/add":
        title = one("title").strip()[:60]
        if title:
            st["next_id"] += 1
            tasks.insert(0, {"id": st["next_id"], "title": title, "due": one("due")[:10], "prio": one("prio") or "보통", "status": "todo", "memo": one("memo")[:200],
                             "star": False, "assignee": "", "tags": one("tags")[:100]})
            st["log"].append(["add", st["next_id"]])
    elif path == "/board/update":
        t = find()
        if t:
            for k in ("title", "due", "prio", "memo", "status", "tags"):
                if k in form:
                    t[k] = one(k)[:200]
            st["log"].append(["update", t["id"], sorted(k for k in form if k != "id")])
    elif path == "/board/toggle":
        t = find()
        if t:
            t["status"] = "todo" if t["status"] == "done" else "done"
            st["log"].append(["toggle", t["id"]])
    elif path == "/board/delete":
        t = find()
        if t:
            tasks.remove(t)
            st["log"].append(["delete", t["id"]])
    elif path == "/board/star":
        t = find()
        if t:
            t["star"] = not t["star"]
            st["log"].append(["star", t["id"]])
    elif path == "/board/assign":   # 목록에 있는 이름만 받는다(추천 목록에서 골라야 한다)
        t = find()
        if t and one("name") in PEOPLE:
            t["assignee"] = one("name")
            st["log"].append(["assign", t["id"]])
    elif path == "/board/sort":
        if one("by") in ("none", "due", "prio"):
            st["sort"] = one("by")
            st["log"].append(["sort", one("by")])
    elif path == "/board/note":
        st["note"] = one("note")[:500]
        st["log"].append(["note"])
        return ("<!doctype html><html lang='ko'><head><meta charset='utf-8'><title>공용 메모장</title></head><body style='font:15px sans-serif'>"
                "<p role='status'>메모를 저장했어요.</p><a href='/board/note'>메모장으로</a></body></html>")
    elif path == "/board/archive":
        done = [t for t in tasks if t["status"] == "done"]
        for t in done:
            tasks.remove(t)
        st["archived"] += len(done)
        st["log"].append(["archive", len(done)])
    else:
        return None
    return "{}"


PAGE = r"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>합성 플래너</title>
<style>
body{font:15px sans-serif;margin:0;background:#f6f6f8;color:#222}
header{display:flex;gap:12px;align-items:center;padding:10px 16px;background:#fff;border-bottom:1px solid #ddd;position:sticky;top:0}
button{font:inherit;cursor:pointer} table{width:100%;border-collapse:collapse;background:#fff} td,th{padding:8px;border-bottom:1px solid #eee;text-align:left}
.pop{position:absolute;background:#fff;border:1px solid #999;box-shadow:0 4px 16px #0003;padding:6px;z-index:50;min-width:140px}
.pop [role=menuitem],.pop [role=option]{padding:6px 10px;cursor:pointer}.pop [role=menuitem]:hover,.pop [role=option]:hover{background:#eef}
.veil{position:fixed;inset:0;background:#0006;z-index:40;display:flex;align-items:center;justify-content:center}
.sheet{background:#fff;padding:20px;width:380px;border-radius:10px;position:relative}
.sheet input[type=text]{width:100%;padding:8px;box-sizing:border-box;margin:6px 0}
.memo{min-height:60px;border:1px solid #bbb;padding:8px;margin:6px 0}
.cal{display:grid;grid-template-columns:repeat(7,34px);gap:2px}.cal button{height:30px;border:0;background:#f2f2f2}.cal button.on{background:#36c;color:#fff}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#222;color:#fff;padding:8px 16px;border-radius:20px}
.cols{display:flex;gap:12px;padding:12px}.col{flex:1;background:#eceff4;padding:8px;min-height:200px;border-radius:8px}.card{background:#fff;padding:8px;margin:6px 0;border-radius:6px;cursor:grab}
.badge{padding:2px 8px;border-radius:10px;background:#eee;border:0}
.star{visibility:hidden;border:0;background:none;font-size:16px}tr:hover .star,.star.on{visibility:visible}
.acts{height:300px;overflow:auto;background:#fff;margin:12px;border:1px solid #ccc}.acts div{padding:10px 12px;border-bottom:1px solid #eee}
.chip{display:inline-block;background:#dde6ff;border-radius:10px;padding:2px 8px;margin:2px}
</style></head><body>
<header><b>합성 플래너</b><button id="proj" aria-haspopup="menu"></button>
<div role="tablist"><button role="tab" id="tab-list" aria-selected="true">목록</button><button role="tab" id="tab-board" aria-selected="false">보드</button><button role="tab" id="tab-acts" aria-selected="false">기록</button><button role="tab" id="tab-note" aria-selected="false">메모장</button></div>
<input type="text" id="q" placeholder="할 일 검색" style="margin-left:auto"><button id="new">＋ 새 할 일</button></header>
<main id="main"></main><div id="layer"></div>
<script>
const $ = (s, r) => (r || document).querySelector(s), layer = $('#layer'), main = $('#main');
let S = {project: '', projects: [], tasks: [], sort: 'none', people: []}, shown = 10, view = 'list', query = '', prioOnly = '';
const PR = {'높음': 0, '보통': 1, '낮음': 2};
// 우선순위 거르기: 웹 컴포넌트(그림자 DOM 안의 단추)
customElements.define('prio-filter', class extends HTMLElement { connectedCallback() { const r = this.attachShadow({mode: 'open'});
  r.innerHTML = '<span>우선순위 거르기 </span>' + ['전체', '높음', '보통', '낮음'].map(x => `<button>${x}</button>`).join(' ');
  r.querySelectorAll('button').forEach(b => b.onclick = () => { prioOnly = b.textContent === '전체' ? '' : b.textContent; shown = 10; draw(); }); } });
const ST = {todo: '할 일', doing: '진행 중', done: '완료'};
const day = d => d ? `${+d.slice(5, 7)}월 ${+d.slice(8, 10)}일` : '—';
const post = (p, o) => fetch(p, {method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: new URLSearchParams(o)}).then(load);
const load = () => fetch('/board/state').then(r => r.text()).then(t => { S = JSON.parse(t); draw(); });
const toast = m => { const e = document.createElement('div'); e.className = 'toast'; e.setAttribute('role', 'status'); e.textContent = m; document.body.append(e); setTimeout(() => e.remove(), 2500); };
const closePops = () => layer.querySelectorAll('.pop').forEach(e => e.remove());
function pop(anchor, build) { closePops(); const r = anchor.getBoundingClientRect(), p = document.createElement('div'); p.className = 'pop';
  p.style.left = (r.left + scrollX) + 'px'; p.style.top = (r.bottom + scrollY + 4) + 'px'; build(p); layer.append(p); return p; }
function menu(anchor, items, role) { return pop(anchor, p => { p.setAttribute('role', role === 'option' ? 'listbox' : 'menu');
  for (const [label, fn] of items) { const i = document.createElement('div'); i.setAttribute('role', role || 'menuitem'); i.tabIndex = 0; i.textContent = label;
    i.onclick = () => { closePops(); fn(); }; p.append(i); } }); }
function calendar(anchor, value, pick) { let ym = (value || '2026-11-15').slice(0, 7);
  const p = pop(anchor, () => {}); p.setAttribute('role', 'dialog'); p.setAttribute('aria-label', '날짜 고르기');
  const render = () => { const [y, m] = ym.split('-').map(Number), first = new Date(y, m - 1, 1).getDay(), n = new Date(y, m, 0).getDate();
    p.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px"><button aria-label="이전 달">‹</button><b>${y}년 ${m}월</b><button aria-label="다음 달">›</button></div><div class="cal"></div><button class="none" style="margin-top:6px">날짜 없음</button>`;
    const g = $('.cal', p); for (const w of '일월화수목금토') { const h = document.createElement('span'); h.textContent = w; h.style.textAlign = 'center'; g.append(h); }
    for (let i = 0; i < first; i++) g.append(document.createElement('span'));
    for (let d = 1; d <= n; d++) { const b = document.createElement('button'), v = `${ym}-${String(d).padStart(2, '0')}`; b.textContent = d; if (v === value) b.className = 'on';
      b.onclick = () => { closePops(); pick(v); }; g.append(b); }
    const [prev, next] = p.querySelectorAll('button[aria-label]'); const move = k => { const t = new Date(y, m - 1 + k, 1); ym = `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, '0')}`; render(); };
    prev.onclick = () => move(-1); next.onclick = () => move(1); $('.none', p).onclick = () => { closePops(); pick(''); }; };
  render(); }
function sheet(task) {   // 새 할 일·수정: 창 위에 뜨는 입력 창. 칸에 이름표가 없다(자리 글만)
  const t = task ? {...task} : {title: '', due: '', prio: '보통', memo: ''};
  const v = document.createElement('div'); v.className = 'veil';
  v.innerHTML = `<div class="sheet" role="dialog" aria-modal="true" aria-label="${task ? '할 일 수정' : '새 할 일'}"><h2>${task ? '할 일 수정' : '새 할 일'}</h2>
    <input type="text" class="title" placeholder="제목"><div><button class="due"></button> <button class="prio" aria-haspopup="listbox"></button></div>
    <div class="memo" contenteditable="true" role="textbox" aria-label="메모"></div>
    <div class="tags"></div><input type="text" class="tag" placeholder="태그 입력 후 Enter">
    <div style="text-align:right"><button class="cancel">취소</button> <button class="save">저장</button></div></div>`;
  let tags = (t.tags || '').split(',').filter(Boolean);
  const chips = () => { $('.tags', v).innerHTML = ''; for (const x of tags) { const c = document.createElement('span'); c.className = 'chip'; c.textContent = '#' + x; $('.tags', v).append(c); } };
  chips(); $('.tag', v).onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); const x = e.target.value.trim(); if (x && !tags.includes(x)) tags.push(x); e.target.value = ''; chips(); } };
  const due = $('.due', v), prio = $('.prio', v), paint = () => { due.textContent = '마감일: ' + (t.due ? day(t.due) : '없음'); prio.textContent = '우선순위: ' + t.prio + ' ▾'; };
  $('.title', v).value = t.title; $('.memo', v).textContent = t.memo; paint();
  due.onclick = () => calendar(due, t.due, d => { t.due = d; paint(); });
  prio.onclick = () => menu(prio, ['높음', '보통', '낮음'].map(x => [x, () => { t.prio = x; paint(); }]), 'option');
  $('.cancel', v).onclick = () => { closePops(); v.remove(); };
  $('.save', v).onclick = () => { const title = $('.title', v).value.trim(); if (!title) { toast('제목을 입력해 주세요'); return; }
    const o = {title, due: t.due, prio: t.prio, memo: $('.memo', v).innerText.trim(), tags: tags.join(',')}; closePops(); v.remove();
    (task ? post('/board/update', {id: task.id, ...o}) : post('/board/add', o)).then(() => toast(task ? '저장했어요' : '할 일을 추가했어요')); };
  layer.append(v); $('.title', v).focus(); }
function confirmDelete(t) { const v = document.createElement('div'); v.className = 'veil';
  v.innerHTML = `<div class="sheet" role="alertdialog" aria-modal="true"><p>'${t.title}' 할 일을 삭제할까요? 되돌릴 수 없어요.</p><div style="text-align:right"><button class="cancel">취소</button> <button class="ok">삭제</button></div></div>`;
  $('.cancel', v).onclick = () => v.remove(); $('.ok', v).onclick = () => { v.remove(); post('/board/delete', {id: t.id}).then(() => toast('삭제했어요')); }; layer.append(v); }
function dueOnly(anchor, t) { let d = t.due; const p = pop(anchor, () => {}); p.setAttribute('role', 'dialog'); p.setAttribute('aria-label', '마감일 바꾸기');
  const paint = () => { p.innerHTML = `<div>마감일: <button class="due">${d ? day(d) : '없음'}</button></div><div style="text-align:right;margin-top:8px"><button class="cancel">취소</button> <button class="save">저장</button></div>`;
    $('.due', p).onclick = e => { const keep = p; calendar(e.target, d, x => { d = x; layer.append(keep); paint(); }); };
    $('.cancel', p).onclick = closePops; $('.save', p).onclick = () => { closePops(); post('/board/update', {id: t.id, due: d}).then(() => toast('마감일을 바꿨어요')); }; };
  paint(); }
function assign(cell, t) { cell.innerHTML = '<input type="text" role="combobox" aria-expanded="false" aria-label="담당자" placeholder="이름 입력">'; const i = $('input', cell); i.focus();
  i.oninput = () => { closePops(); const hit = S.people.filter(n => i.value.trim() && n.includes(i.value.trim())); i.setAttribute('aria-expanded', hit.length > 0);
    if (hit.length) menu(i, hit.map(n => [n, () => post('/board/assign', {id: t.id, name: n}).then(() => toast('담당자를 지정했어요'))]), 'option'); };
  i.onkeydown = e => { if (e.key === 'Escape') { closePops(); draw(); } }; }
let actsOff = 0;
function acts() { main.innerHTML = '<h3 style="padding:0 12px">활동 기록</h3><div class="acts" tabindex="0" aria-label="활동 기록 목록"></div>'; actsOff = 0; const box = $('.acts');
  const more = () => fetch('/board/activity?offset=' + actsOff).then(r => r.text()).then(x => { const d = JSON.parse(x); for (const a of d.items) { const e = document.createElement('div'); e.textContent = a.date + ' · ' + a.text; box.append(e); }
    actsOff += d.items.length; box.dataset.done = actsOff >= d.total ? '1' : ''; });
  box.onscroll = () => { if (!box.dataset.done && box.scrollTop + box.clientHeight >= box.scrollHeight - 10) more(); }; more(); }
function draw() { $('#proj').textContent = '프로젝트: ' + S.project + ' ▾';
  if (view === 'acts') return acts();
  if (view === 'note') { main.innerHTML = '<h3 style="padding:0 12px">공용 메모장</h3><iframe src="/board/note" title="공용 메모장" style="width:420px;height:200px;margin:12px;border:1px solid #ccc"></iframe>'; return; }
  let all = S.tasks.filter(t => (!query || t.title.includes(query)) && (!prioOnly || t.prio === prioOnly));
  if (S.sort === 'due') all = [...all].sort((a, b) => (a.due || '9') < (b.due || '9') ? -1 : 1);
  if (S.sort === 'prio') all = [...all].sort((a, b) => PR[a.prio] - PR[b.prio]);
  if (view === 'board') { main.innerHTML = '<div class="cols"></div>';
    for (const k of Object.keys(ST)) { const c = document.createElement('section'); c.className = 'col'; c.dataset.s = k; c.innerHTML = `<h3>${ST[k]} ${all.filter(t => t.status === k).length}</h3>`;
      c.ondragover = e => e.preventDefault(); c.ondrop = e => { e.preventDefault(); post('/board/update', {id: e.dataTransfer.getData('text'), status: k}); };
      for (const t of all.filter(t => t.status === k)) { const d = document.createElement('div'); d.className = 'card'; d.draggable = true; d.textContent = t.title;
        d.ondragstart = e => e.dataTransfer.setData('text', t.id); c.append(d); }
      $('.cols').append(c); } return; }
  const rows = all.slice(0, shown);
  main.innerHTML = `<div style="padding:8px 16px;display:flex;gap:16px;align-items:center"><prio-filter></prio-filter>
    <label>정렬 <select id="sort"><option value="none">기본순</option><option value="due">마감 빠른 순</option><option value="prio">우선순위순</option></select></label>
    <button id="arch">완료된 할 일 보관</button></div>
    <table><thead><tr><th></th><th>할 일</th><th>마감</th><th>우선순위</th><th>상태</th><th>담당자</th><th></th></tr></thead><tbody></tbody></table><p style="padding:8px 16px">전체 ${all.length}개 중 ${rows.length}개 표시 <button id="more"${rows.length < all.length ? '' : ' hidden'}>더 보기 (${all.length - rows.length}개 남음)</button></p>`;
  for (const t of rows) { const tr = document.createElement('tr');
    tr.innerHTML = `<td><input type="checkbox" aria-label="완료 표시"${t.status === 'done' ? ' checked' : ''}></td><td class="tt"></td><td>${day(t.due)}</td><td>${t.prio}</td><td><button class="badge">${ST[t.status]}</button></td><td class="who"><button class="as">${t.assignee || '지정'}</button></td><td><button class="star${t.star ? ' on' : ''}" aria-label="별표" aria-pressed="${t.star}">${t.star ? '★' : '☆'}</button> <button class="dots" aria-label="더 보기 메뉴">⋯</button></td>`;
    $('.as', tr).onclick = () => assign($('.who', tr), t);
    $('.star', tr).onclick = () => post('/board/star', {id: t.id});
    $('.tt', tr).textContent = t.title + (t.memo ? ' 📝' : '') + (t.tags ? ' ' + t.tags.split(',').map(x => '#' + x).join(' ') : '');
    $('input', tr).onchange = () => post('/board/toggle', {id: t.id});
    $('.badge', tr).onclick = e => menu(e.target, Object.entries(ST).map(([k, v]) => [v, () => post('/board/update', {id: t.id, status: k})]));
    $('.dots', tr).onclick = e => menu(e.target, [['수정', () => sheet(t)], ['마감일 바꾸기', () => dueOnly(e.target, t)], ['삭제', () => confirmDelete(t)]]);
    $('tbody').append(tr); }
  const more = $('#more'); if (more) more.onclick = () => { shown += 10; draw(); };
  $('#sort').value = S.sort; $('#sort').onchange = e => post('/board/sort', {by: e.target.value});
  $('#arch').onclick = () => { const n = S.tasks.filter(t => t.status === 'done').length; if (confirm(`완료된 할 일 ${n}개를 보관할까요? 목록에서 사라져요.`)) post('/board/archive', {}).then(() => toast('보관했어요')); }; }
$('#proj').onclick = e => menu(e.target, S.projects.map(p => [p, () => { shown = 10; post('/board/project', {name: p}); }]));
$('#new').onclick = () => sheet(null);
$('#q').oninput = e => { query = e.target.value.trim(); shown = 10; draw(); };
const TABS = [['tab-list', 'list'], ['tab-board', 'board'], ['tab-acts', 'acts'], ['tab-note', 'note']];
for (const [id, v] of TABS) $('#' + id).onclick = () => { view = v; closePops(); for (const [i2, v2] of TABS) $('#' + i2).setAttribute('aria-selected', v2 === v); draw(); };
document.addEventListener('keydown', e => { if (e.key === 'Escape') closePops(); });
load();
</script></body></html>"""
