"""斜杠命令注册表：命令与处理函数的唯一事实来源。

注意：本模块只提供注册机制，命令的执行体在 commands.py 中定义，
需要由入口（main.py）import commands 触发 @command 装饰器完成注册——
没有导入，COMMANDS 永远是空表。
"""

COMMANDS: dict = {}

def command(name: str, description: str):
    def decorator(func):
        func.description = description
        COMMANDS[name] = func
        return func
    return decorator
