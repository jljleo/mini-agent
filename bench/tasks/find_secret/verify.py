from pathlib import Path

answer = Path("answer.txt").read_text(encoding="utf-8").strip()
assert "delta-42" in answer, f"答案错误: {answer!r}"
assert "alpha-19" not in answer, f"拿到了作废密钥: {answer!r}"
print("verify OK")
