"""入口：演示分页。"""

from paginate import paginate


def main() -> None:
    items = list(range(1, 26))
    page, total = paginate(items, page=3, size=10)
    print(f"第 3/{total} 页: {page}")


if __name__ == "__main__":
    main()
