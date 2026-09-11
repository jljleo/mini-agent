"""连接池。"""


class Connection:
    def __init__(self, ok: bool = True) -> None:
        self._ok = ok

    def ping(self) -> bool:
        return self._ok


class Pool:
    """固定容量连接池：acquire/release 配对使用。"""

    def __init__(self, size: int = 2) -> None:
        self._free: list[Connection] = [Connection() for _ in range(size)]
        self._busy: set[int] = set()

    def acquire(self) -> Connection:
        if not self._free:
            raise RuntimeError("连接池耗尽")
        conn = self._free.pop()
        self._busy.add(id(conn))
        return conn

    def release(self, conn: Connection) -> None:
        if id(conn) not in self._busy:
            raise RuntimeError("重复释放")
        self._busy.discard(id(conn))
        self._free.append(conn)
