"""验证「重依赖延迟导入」没被改回去。纯离线，不碰网络 / 数据库 / mpv。

背景（2026-11-15 实测，HDD 冷启动）：
    改前 `import main_CUI` = 32.19 s
      - main_CUI.py 顶层 `from VoiceRecognition import ...` -> funasr + torch
      - initial_setup.py 顶层 `from modelscope import ...`  -> 又拉一次 torch
    改后 = 0.48 s，四个重模块都不在 sys.modules 里。
  这两处分别只有 `voice` 命令、首次运行向导的自定义分支才用得到，
  却让每次启动都付一遍冷读成本。

检查两层：
  1. AST 静态：顶层不许有这两个 import；它们必须落到对应函数里，
     且 VoiceRecognition 那个要被 try 包住（缺依赖时给正常提示，
     而不是子线程甩 traceback）。
  2. 子进程实跑 `import main_CUI`：torch / funasr / modelscope /
     VoiceRecognition 都不该被加载。

用法：
    python lazy_import_verify.py
    python lazy_import_verify.py --demo     # 附带说明和实测耗时
"""
import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HEAVY = ("torch", "funasr", "modelscope", "VoiceRecognition")


def parse(name):
    return ast.parse((ROOT / name).read_text(encoding="utf-8"), filename=name)


def module_imports(tree):
    """只看模块顶层（tree.body），函数里的不算。"""
    out = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            out.append(node.module or "")
    return out


def find_func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def imports_inside(func, module):
    hits = []
    for node in ast.walk(func):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == module:
                hits.append(node)
        elif isinstance(node, ast.Import):
            if any(a.name.split(".")[0] == module for a in node.names):
                hits.append(node)
    return hits


def try_covered(func):
    """被某个 try: 的 body 包住的所有节点 id。"""
    covered = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Try):
            for stmt in node.body:
                for sub in ast.walk(stmt):
                    covered.add(id(sub))
    return covered


def runtime_probe():
    code = (
        "import json,sys,time;"
        "t=time.perf_counter();import main_CUI;d=time.perf_counter()-t;"
        "print('__RESULT__'+json.dumps({"
        "'elapsed':round(d,2),"
        "'loaded':[m for m in ['torch','funasr','modelscope','VoiceRecognition'] "
        "if m in sys.modules]}))"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                       capture_output=True, text=True, timeout=180)
    for line in (r.stdout or "").splitlines():
        if line.startswith("__RESULT__"):
            return json.loads(line[len("__RESULT__"):])
    raise RuntimeError("子进程没回结果: rc=%s stderr=%s" %
                       (r.returncode, (r.stderr or "")[-400:]))


def main():
    ap = argparse.ArgumentParser(description="延迟导入回归检查")
    ap.add_argument("--demo", action="store_true", help="打印说明与实测耗时")
    args = ap.parse_args()

    checks = []

    def check(name, ok, detail=""):
        checks.append((name, ok, detail))

    cui = parse("main_CUI.py")
    setup = parse("initial_setup.py")

    top_cui = module_imports(cui)
    top_setup = module_imports(setup)

    check("main_CUI 顶层不再导入 VoiceRecognition",
          not any(m.split(".")[0] == "VoiceRecognition" for m in top_cui))
    check("initial_setup 顶层不再导入 modelscope",
          not any(m.split(".")[0] == "modelscope" for m in top_setup))

    cmd_voice = find_func(cui, "cmd_voice")
    if cmd_voice is None:
        check("cmd_voice 内保留延迟导入", False, "找不到 cmd_voice")
        check("延迟导入被 try 包住", False, "找不到 cmd_voice")
    else:
        hits = imports_inside(cmd_voice, "VoiceRecognition")
        check("cmd_voice 内保留延迟导入", len(hits) == 1,
              "找到 %d 处" % len(hits))
        covered = try_covered(cmd_voice)
        check("延迟导入被 try 包住", bool(hits) and all(id(h) in covered for h in hits),
              "缺依赖时走正常错误提示而非子线程 traceback")

    preload = find_func(setup, "_preload_models")
    check("_preload_models 内保留 modelscope 延迟导入",
          preload is not None and len(imports_inside(preload, "modelscope")) == 1)

    try:
        probe = runtime_probe()
        check("子进程 import main_CUI 不加载重依赖", not probe["loaded"],
              "elapsed=%.2fs loaded=%s" % (probe["elapsed"], probe["loaded"] or "无"))
    except Exception as e:
        check("子进程 import main_CUI 不加载重依赖", False, str(e))

    if args.demo:
        print("=" * 66)
        print("延迟导入：改前 32.19 s -> 改后约 0.5 s（HDD 冷启动实测）")
        print("=" * 66)
        print("原则：funasr / modelscope / torch 只在真正需要它们的那条")
        print("命令里 import。程序随开随用时，启动慢最影响体感。")
        print("注意：voice 命令本身仍要付模型加载的时间，这里省的是")
        print("      「不用 voice 时白付的那一份」。\n")

    print("-" * 66)
    for name, ok, detail in checks:
        print("  [%s] %s%s" % ("OK  " if ok else "FAIL", name,
                              ("  " + detail) if detail else ""))
    passed = sum(1 for _, ok, _ in checks if ok)
    print("-" * 66)
    print("结果：%d/%d 通过" % (passed, len(checks)))
    print("-" * 66)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
