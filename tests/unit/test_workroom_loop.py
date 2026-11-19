"""작업실 루프의 선(D96): 폴더 밖 경로 거절, finish의 파일 거름, 상한에서 멈춤. 컨테이너 없이 가짜 방으로."""

from pathlib import Path

from agent.providers.base import ModelResponse, ToolCall
from agent.workroom.loop import WorkroomLoop, _inside
from agent.workroom.manager import RunResult


class Room:
    def __init__(self, path: Path):
        self.path, self.ran = path, []

    def run(self, cmd, t=120):
        self.ran.append(cmd)
        return RunResult(0, "ok", 0.1)


class Scripted:
    model = "claude-sonnet-5-5"

    def __init__(self, *calls, cost_per_call=0):
        self.calls, self.seen = list(calls), []
        self.totals = {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        self.cost = cost_per_call

    def generate(self, msgs, **kw):
        self.seen.append(list(msgs))
        self.totals["output"] += self.cost
        name, args = self.calls.pop(0) if self.calls else ("finish", {"summary": "끝", "files": []})
        return ModelResponse(text="", tool_calls=[ToolCall(name=name, arguments=args, id=f"t{len(self.seen)}")],
                             finish_reason="tool_calls", latency_ms=1, model=self.model, provider="fake")


def test_paths_stay_inside_the_project_folder(tmp_path):
    assert _inside(tmp_path, "/work/a/b.txt") == (tmp_path / "a/b.txt").resolve()
    assert _inside(tmp_path, "../../etc/passwd") is None and _inside(tmp_path, "/work/../x") is None
    room = Room(tmp_path)
    p = Scripted(("write_file", {"path": "/work/s.py", "content": "print(1)"}),
                 ("write_file", {"path": "/work/../../.env", "content": "x"}),
                 ("read_file", {"path": "/etc/hosts"}),
                 ("run", {"command": "python3 s.py"}),
                 ("finish", {"summary": "만들었다", "files": ["/work/s.py", "/work/없는.mp4", "/work/../../.zshrc"]}))
    out = WorkroomLoop(room, p).run("시험")
    assert (tmp_path / "s.py").read_text() == "print(1)" and not (tmp_path.parent.parent / ".env").exists()
    assert [s.ok for s in out.steps] == [True, False, False, True]
    assert (tmp_path / "etc/hosts").exists() is False and "밖은 다룰 수 없다" in p.seen[2][-1].content
    assert out.status == "completed" and out.files == ["/work/s.py"]                  # 없는 파일·바깥 파일은 결과로 주지 않는다


def test_it_stops_at_the_budget_and_step_limits(tmp_path):
    p = Scripted(*[("run", {"command": "true"})] * 50, cost_per_call=200_000)          # 한 번에 $2
    out = WorkroomLoop(Room(tmp_path), p, max_usd=3.0).run("시험")
    assert out.status == "budget" and len(out.steps) == 2
    out = WorkroomLoop(Room(tmp_path), Scripted(*[("run", {"command": "true"})] * 50), max_steps=5).run("시험")
    assert out.status == "steps" and len(out.steps) == 5
