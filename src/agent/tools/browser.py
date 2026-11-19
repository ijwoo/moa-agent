"""브라우저 도구 정의 (Phase 2: 정의·검증만. 실제 실행은 Phase 3)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .spec import ToolRegistry, ToolSpec

REF_PATTERN = r"^e\d+$"


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NavigateArgs(_Args):
    url: str = Field(description="이동할 전체 URL (http:// 또는 https://)", max_length=2048)


class ObserveArgs(_Args):
    pass


class ClickArgs(_Args):
    ref: str = Field(description="관찰 결과에 나온 요소 ref (예: e3)", pattern=REF_PATTERN)


class TypeArgs(_Args):
    ref: str = Field(description="입력할 요소 ref (예: e2)", pattern=REF_PATTERN)
    text: str = Field(description="입력할 내용", max_length=5000)
    submit: bool = Field(default=False, description="입력 후 Enter를 누를지 여부")


class SelectOptionArgs(_Args):
    ref: str = Field(description="선택 상자(combobox) ref", pattern=REF_PATTERN)
    option: str = Field(description="고를 옵션의 이름 (관찰 결과에 보이는 그대로)", min_length=1, max_length=200)


class ScrollArgs(_Args):
    direction: Literal["up", "down"] = Field(description="스크롤 방향")


class PressKeyArgs(_Args):
    ref: str = Field(description="키를 받을 요소 ref (보통 입력 칸)", pattern=REF_PATTERN)
    key: Literal["Enter", "Escape", "Tab", "ArrowDown", "ArrowUp"] = Field(description="누를 키")


class HoverArgs(_Args):
    ref: str = Field(description="마우스를 올릴 요소 ref", pattern=REF_PATTERN)


class FindInPageArgs(_Args):
    text: str = Field(description="화면에서 찾을 낱말(짧게: 이름·날짜·번호 등)", min_length=1, max_length=80)


class DragArgs(_Args):
    text: str = Field(description="끌 것의 화면 글(카드의 제목 등). 관찰 결과에 보이는 그대로", min_length=1, max_length=200)
    to_ref: str = Field(description="놓을 곳의 요소 ref (칸의 머리글 등)", pattern=REF_PATTERN)


class ExtractTextArgs(_Args):
    ref: str | None = Field(
        default=None, description="텍스트를 읽을 요소 ref. 페이지 전체면 null", pattern=REF_PATTERN
    )


class SearchWebArgs(_Args):
    query: str = Field(description="검색어", min_length=1, max_length=500)


class FinishArgs(_Args):
    summary: str = Field(description="한 일과 결과, 또는 질문에 대한 답. 실제로 확인한 내용만", min_length=1, max_length=2000)


class AskUserArgs(_Args):
    question: str = Field(description="사용자에게 확인할 질문", min_length=1, max_length=1000)


BROWSER_TOOLS: list[ToolSpec] = [
    ToolSpec("navigate", "주어진 URL로 이동한다. 페이지에 링크가 보이면 navigate 대신 click을 쓴다.", NavigateArgs),
    ToolSpec("observe", "현재 페이지의 URL, 제목, 클릭·입력 가능한 요소 목록(ref)을 다시 본다.", ObserveArgs),
    ToolSpec("click", "관찰 결과의 요소(ref)를 클릭한다.", ClickArgs),
    ToolSpec("type", "관찰 결과의 입력 요소(ref)에 텍스트를 입력한다. 그 칸의 기존 내용은 지워지고 text로 바뀐다 "
                     "(내용을 덧붙이려면 기존 내용까지 포함한 전체 문장을 text에 쓴다).", TypeArgs),
    ToolSpec("scroll", "페이지를 위나 아래로 스크롤한다.", ScrollArgs),
    ToolSpec("extract_text", "현재 페이지나 특정 요소의 텍스트를 읽어 온다.", ExtractTextArgs),
    ToolSpec("search_web", "웹 검색을 한다. 어느 페이지로 가야 할지 모를 때 쓴다.", SearchWebArgs),
    ToolSpec("ask_user", "필요한 정보가 빠졌거나 요청이 모호할 때 사용자에게 묻는다.", AskUserArgs),
]


SELECT_TOOL = ToolSpec("select_option", "선택 상자(combobox)에서 옵션 하나를 고른다.", SelectOptionArgs)
# 복잡한 화면용(D72 — 합성 플래너 측정에서 없어서 못 한 동작들). 화면을 보는 모델(Claude)이 운전하는 길에서만 준다
PRESS_KEY_TOOL = ToolSpec("press_key", "입력 칸에서 키 하나를 누른다. Enter로 넣는 칸(태그·빠른 추가)에는 type으로 글자를 넣은 뒤 이 도구로 Enter를 누른다 "
                                       "(검색창이 아니면 사용자에게 승인을 묻는다). Escape는 열린 메뉴·창을 닫는다.", PressKeyArgs)
HOVER_TOOL = ToolSpec("hover", "요소 위에 마우스를 올린다. 마우스를 올려야 보이는 단추·메뉴가 있을 때 쓴다(올린 뒤의 화면이 돌아온다).", HoverArgs)
DRAG_TOOL = ToolSpec("drag", "화면의 것(카드 등)을 끌어 다른 곳(칸)에 놓는다. 끌 것은 화면 글로, 놓을 곳은 ref로 준다.", DragArgs)
FIND_TOOL = ToolSpec("find_in_page", "지금 화면 전체의 글에서 낱말을 찾아, 찾은 곳마다 앞뒤 글과 함께 돌려준다. 화면이 길어 관찰 결과나 extract_text가 잘렸을 때, "
                                     "찾는 값이 어디 있는지 모를 때 쓴다(스크롤을 되풀이하는 것보다 빠르다).", FindInPageArgs)
RICH_TOOLS = [PRESS_KEY_TOOL, HOVER_TOOL, DRAG_TOOL, FIND_TOOL]


# 내려받은 표(D125, 사용자 결정 2026-11-18): 화면에 다 안 나오고 CSV·엑셀로만 전체를 주는 공개 통계·자료 사이트용.
# 모델은 코드를 쓰지 않는다 — 정해 둔 거르기·정렬·합계만 고르고, 계산은 브라우저 서비스(맥 쪽 코드)가 한다
class DownloadTableArgs(_Args):
    ref: str = Field(description="누르면 CSV·엑셀 파일을 내려받는 단추·링크의 ref", pattern=REF_PATTERN)


class TableCond(_Args):
    col: str = Field(description="열 이름(download_table이 알려 준 그대로)", min_length=1, max_length=80)
    op: Literal["=", "!=", ">", ">=", "<", "<=", "contains"] = Field(description="비교")
    value: str = Field(description="비교할 값(숫자도 글로)", max_length=200)


class TableQueryArgs(_Args):
    table_id: str = Field(description="download_table이 준 표 id", pattern=r"^t\d{1,3}$")
    where: list[TableCond] = Field(default_factory=list, max_length=8, description="모두 만족하는 줄만(없으면 전부)")
    sort_by: str | None = Field(default=None, max_length=80, description="정렬할 열(숫자면 숫자로 견준다)")
    desc: bool = Field(default=True, description="큰 것부터(true) / 작은 것부터(false)")
    limit: int = Field(default=10, ge=1, le=50, description="돌려줄 줄 수")
    columns: list[str] = Field(default_factory=list, max_length=12, description="보여 줄 열(없으면 전부)")
    agg: Literal["", "count", "sum", "avg", "max", "min"] = Field(default="", description="거른 줄 전체의 합계·평균 등(그 열은 agg_col)")
    agg_col: str | None = Field(default=None, max_length=80)
    total_of: list[str] = Field(default_factory=list, max_length=6,
                                description="줄마다 이 열들을 더한 '합계' 열을 만든다(한 값이 여러 열로 나뉜 표 — 예: 거치대 LCD + QR). 그 뒤 sort_by·where·columns에 '합계'를 쓸 수 있다")


DOWNLOAD_TABLE_TOOL = ToolSpec("download_table", "화면의 내려받기(CSV·엑셀) 단추를 눌러 파일을 받아 표로 읽는다 — 열 이름·줄 수·앞의 몇 줄을 돌려준다. "
                                                 "화면 표가 여러 쪽으로 나뉘거나 너무 커서 다 읽기 어려울 때 쓴다(공개 자료만). "
                                                 "먼저 파일 형식(CSV·엑셀)을 고르는 창이 뜨는 사이트가 많다 — 그 창에서 CSV를 고른 뒤 마지막 내려받기 단추에 쓴다.", DownloadTableArgs)
TABLE_QUERY_TOOL = ToolSpec("table_query", "download_table로 받은 표에서 조건으로 거르고, 정렬하고, 위의 몇 줄이나 합계·평균을 돌려준다. "
                                           "계산은 맥이 한다 — 표의 값을 직접 더하거나 견주지 말고 이 도구를 쓴다.", TableQueryArgs)
TABLE_TOOLS = [DOWNLOAD_TABLE_TOOL, TABLE_QUERY_TOOL]
FINISH_TOOL = ToolSpec("finish", "작업을 끝냈거나 답을 알았을 때 호출한다. 결과·답을 summary에 쓴다.", FinishArgs)


class AgentAskUserArgs(_Args):
    question: str = Field(description="사용자에게 확인할 질문", min_length=1, max_length=1000)
    choices: list[str] = Field(default_factory=list, max_length=4,
                               description="사용자가 고를 수 있는 답 후보 2~4개(없으면 빈 배열)")


class AgentFinishArgs(_Args):
    summary: str = Field(description="결과나 답을 한두 문장으로. 실제로 확인한 내용만. 저장한 초안의 본문을 다시 옮겨 쓰지 않는다",
                         min_length=1, max_length=2000)
    done: list[str] = Field(default_factory=list, description="실제로 한 일 목록(짧게)")
    blocked: list[str] = Field(default_factory=list, description="하지 못한 일과 그 이유(없으면 빈 배열)")


# 3단계(D45): 작업 담당 전용 — 질문에 선택지, 완료 보고를 '한 일 / 못 한 일'로. Phase 2 평가용 default_registry는 그대로 둔다
AGENT_ASK_TOOL = ToolSpec("ask_user", "필요한 정보가 빠졌거나 요청이 모호할 때 사용자에게 묻는다. 고를 수 있는 답이 있으면 choices에 넣는다.",
                          AgentAskUserArgs)
AGENT_FINISH_TOOL = ToolSpec("finish", "작업을 끝냈거나 답을 알았을 때 호출한다. summary에 결과, done에 한 일, blocked에 못 한 일과 이유.",
                             AgentFinishArgs)


class ComposeMailArgs(_Args):
    to: str = Field(description="받는 사람 메일 주소 하나 (작업에 적힌 받는 사람 그대로)", max_length=254,
                    pattern=r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
    subject: str = Field(description="메일 제목 (한 줄)", min_length=1, max_length=200)
    body: str = Field(description="메일 본문 전체. 담을 내용을 직접 쓴다(첨부·다른 문서를 가리키지 않는다)", min_length=1, max_length=20000)


# 4단계(D42): 실제 발송은 이 도구 하나로만. 호출하면 서버가 초안을 고정하고 사용자 승인 → 대기 → 발송 → 도착 확인을 한다
COMPOSE_MAIL_TOOL = ToolSpec(
    "compose_mail",
    "작업에 적힌 받는 사람에게 실제 메일을 보낸다(브라우저를 쓰지 않는다). 호출하면 시스템이 사용자 승인을 받은 뒤 보내고 도착을 확인한다. "
    "사용자가 고칠 점을 말하면 고친 전체 내용으로 다시 호출한다.", ComposeMailArgs, timeout_s=120.0)


def default_registry() -> ToolRegistry:
    """Phase 2 평가와 같은 도구 8개 (재현성을 위해 바꾸지 않는다)."""
    return ToolRegistry(BROWSER_TOOLS)


def agent_registry(send: bool = False, rich: bool = False, tables: bool = False) -> ToolRegistry:
    """Agent Loop용: 브라우저 도구 8개 + select_option + finish. send=True면 compose_mail(본인 주소 발송)도.
    rich=True면 복잡한 화면용 도구(press_key·hover·drag)도 — 로컬 모델의 길에는 주지 않는다(도구가 늘면 고르기가 어려워진다)."""
    tools = [AGENT_ASK_TOOL if t.name == "ask_user" else t for t in BROWSER_TOOLS]
    return ToolRegistry([*tools, SELECT_TOOL, *(RICH_TOOLS if rich else []), *(TABLE_TOOLS if tables else []), AGENT_FINISH_TOOL,
                         *([COMPOSE_MAIL_TOOL] if send else [])])
