"""서버를 띄우는 코드(cli.py)가 없는 이름을 쓰지 않는가.

2026-11-15: `_cmd_serve`에 다른 함수의 변수(base)를 쓴 줄을 넣어 서버가 뜨자마자 NameError로 죽었다(2분 반 동안 꺼져 있었다).
단위 테스트는 그 함수를 돌리지 않아 통과했다. 함수마다 '읽는 이름'이 그 함수 안에서 정해지거나 모듈·내장에 있는지 본다.
"""

import ast
import builtins
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "agent"


def undefined_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    top = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}
    for node in tree.body:
        for n in ast.walk(node) if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else [node]:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                top.add(n.name)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                top.add(n.id)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                top.update((a.asname or a.name).split(".")[0] for a in n.names)
    out = []
    for fn in [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        known = set(top)
        for n in ast.walk(fn):
            if isinstance(n, ast.arg):
                known.add(n.arg)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                known.add(n.id)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                known.update((a.asname or a.name).split(".")[0] for a in n.names)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                known.add(n.name)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                known.add(n.name)
            elif isinstance(n, (ast.Global, ast.Nonlocal)):
                known.update(n.names)
        out += [f"{fn.name}:{n.lineno} {n.id}" for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in known]
    return out


@pytest.mark.parametrize("name", ["cli.py", "api/runner.py", "api/server.py"])
def test_functions_do_not_read_names_that_are_never_defined(name):
    assert undefined_names(SRC / name) == []


def test_the_check_catches_a_variable_borrowed_from_another_function(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("import os\n\ndef a():\n    base = os.getcwd()\n    return base\n\ndef b():\n    return base + '/x'\n")
    assert undefined_names(f) == ["b:8 base"]
