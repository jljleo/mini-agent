"""斜杠命令注册表：命令与处理函数的唯一事实来源。

handler 签名统一为 (session, args)；无参数命令忽略 args 即可。

注意：本模块只提供注册机制，命令的执行体在 commands.py 中定义，
需要由入口（main.py）import commands 触发 @command 装饰器完成注册——
没有导入，COMMANDS 永远是空表。
"""

import ui

COMMANDS: dict = {}


def command(name: str, description: str):
    def decorator(func):
        if name in COMMANDS:
            # 重名静默覆盖容易藏 bug（如复制粘贴忘改名），显式告警（走 stderr，不污染 stdout 管道）
            ui.err_console.print(f"[warning]⚠ 斜杠命令重复注册，将被覆盖: {name}[/]")
        func.description = description
        COMMANDS[name] = func
        return func
    return decorator
