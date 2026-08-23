import sys

sys.path.insert(0, ".")  # cwd = 沙箱（run_bench 以沙箱为 cwd 运行本脚本）

from fizzbuzz import fizzbuzz

got = fizzbuzz(15)
expect = [1, 2, "Fizz", 4, "Buzz", "Fizz", 7, 8, "Fizz", "Buzz",
          11, "Fizz", 13, 14, "FizzBuzz"]
assert got == expect, f"fizzbuzz(15) 错误: {got}"
print("verify OK")
