"""led install-hook / uninstall-hook：本地 git 钩子（pre-commit / pre-push 自动 review）。

用户一次安装后零操作：每次 commit（pre-commit）或 push（pre-push）自动跑 led review。
运行在用户自己的机器上、用用户自己的 key（BYOK）——不依赖任何平台、不依赖本仓库
源码（led 是外部安装的命令）。

hook 是 shell 脚本写入 .git/hooks/<kind>（Git 在任何平台都经 sh 执行 hook）。
默认信息性评论（--no-fail）；install --fail-on-high 时 high findings 阻塞
commit/push（本地门禁，E11 的 precision 结论下门禁语义可用在本地自审）。
KIMI_CODE_API_KEY 未设置时静默跳过（不打断用户流程，也不产生误导报错）。

注意：hook 不会自动传播给其他人（git 设计如此，props 是好事不是缺陷）；
卸载用 led uninstall-hook。
"""

from __future__ import annotations

import argparse
import os
import stat

HOOK_DIR = ".git/hooks"

_HOOK_TEMPLATE = """#!/bin/sh
# led code review hook（自动安装于 {installed_at}）
# 作用：{description}
# 配置：export KIMI_CODE_API_KEY 后在 {trigger} 时自动 review。
# 卸载：led uninstall-hook --{kind}
if [ -z "${{KIMI_CODE_API_KEY}}" ]; then
  echo "led review: 跳过（未设置 KIMI_CODE_API_KEY）" >&2
  exit 0
fi
if ! command -v led >/dev/null 2>&1; then
  echo "led review: 跳过（led 未安装，pipx install led-review）" >&2
  exit 0
fi
led {cmd}
"""

_HOOKS = {
    "pre-commit": {
        "description": "每次 git commit 自动 review 工作区改动（git commit 前）",
        "spec": "",
    },
    "pre-push": {
        "description": "每次 git push 自动 review 最近一次提交（git push 前）",
        "spec": "HEAD",
    },
}


def _hook_path(root: str, kind: str) -> str:
    return os.path.join(root, HOOK_DIR, kind)


def install_hook(kind: str, *, fail_on_high: bool, root: str) -> None:
    """写 .git/hooks/<kind> 并加执行位；已存在则覆写（幂等，先删后写）。"""
    if kind not in _HOOKS:
        raise ValueError(f"未知 hook 类型: {kind}（可选: {', '.join(_HOOKS)}）")
    hk = _HOOKS[kind]
    path = _hook_path(root, kind)
    if not os.path.isdir(os.path.join(root, ".git")):
        raise SystemExit(f"✗ {root} 不是 git 仓库（缺 .git/），hook 无法安装")
    os.makedirs(os.path.join(root, HOOK_DIR), exist_ok=True)
    gate = "" if fail_on_high else "--no-fail"
    # 门禁（fail_on_high）：不加 --no-fail → review 默认 high→exit 1 → hook 非零 → 阻塞
    script = _HOOK_TEMPLATE.format(
        installed_at=__import__("datetime").datetime.now().strftime("%Y-%m-%d"),
        description=hk["description"],
        trigger=kind,
        kind=kind,
        cmd=" ".join(x for x in ("review", hk["spec"], gate) if x),
    )
    if os.path.exists(path):
        print(f"led: 已存在 {path}，覆写")
    with open(path, "w", encoding="utf-8") as f:
        f.write(script)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    mode_desc = "门禁（high findings 阻塞）" if fail_on_high else "信息性（不阻塞）"
    print(f"✅ 已安装 {kind} hook → {path}（{mode_desc}）\n"
          f"   下次 {kind} 时自动 led review；卸载: led uninstall-hook --{kind}")


def uninstall_hook(kind: str, *, root: str) -> None:
    """删除 .git/hooks/<kind>；不存在则不报错。"""
    if kind not in _HOOKS:
        raise ValueError(f"未知 hook 类型: {kind}（可选: {', '.join(_HOOKS)}）")
    path = _hook_path(root, kind)
    if os.path.exists(path):
        os.remove(path)
        print(f"🗑  已卸载 {kind} hook（{path}）")
    else:
        print(f"led: {path} 不存在，无需卸载")


def cli(argv: list[str]) -> int:
    """install-hook / uninstall-hook 的 CLI 壳。"""
    parser = argparse.ArgumentParser(
        prog="led hook",
        description="本地 git 钩子：自动 review（一次安装，之后零操作）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    inst = sub.add_parser("install-hook", help="安装自动 review 钩子")
    inst.add_argument("--pre-commit", action="store_true", help="装 pre-commit（commit 前 review 工作区）")
    inst.add_argument("--pre-push", action="store_true", help="装 pre-push（push 前 review 最近提交）")
    inst.add_argument("--fail-on-high", action="store_true", help="high findings 阻塞 commit/push（本地门禁）")
    inst.add_argument("--root", default=os.getcwd(), help="git 仓库根目录（默认当前目录）")

    uninst = sub.add_parser("uninstall-hook", help="卸载钩子")
    uninst.add_argument("--pre-commit", action="store_true")
    uninst.add_argument("--pre-push", action="store_true")
    uninst.add_argument("--root", default=os.getcwd())

    args = parser.parse_args(argv)
    if args.cmd == "install-hook":
        kinds = [k for k in ("pre-commit", "pre-push") if getattr(args, "pre_commit" if k == "pre-commit" else "pre_push")]
        if not kinds:
            kinds = ["pre-push"]  # 缺省装 pre-push（push 前自审最有意义）
        for k in kinds:
            install_hook(k, fail_on_high=args.fail_on_high, root=args.root)
    else:
        kinds = [k for k in ("pre-commit", "pre-push") if getattr(args, "pre_commit" if k == "pre-commit" else "pre_push")]
        if not kinds:
            kinds = ["pre-commit", "pre-push"]
        for k in kinds:
            uninstall_hook(k, root=args.root)
    return 0