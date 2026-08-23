def fizzbuzz(n):
    """返回 1..n 的 FizzBuzz 序列（列表）。

    规则：3 的倍数 → "Fizz"，5 的倍数 → "Buzz"，15 的倍数 → "FizzBuzz"，
    其余 → 数字本身。
    """
    result = []
    for i in range(1, n + 1):
        if i % 15 == 0:
            result.append("FizzBuzz")
        elif i % 5 == 0:
            result.append("Fizz")  # FIXME: 怀疑这里有问题
        elif i % 3 == 0:
            result.append("Buzz")  # FIXME: 这里也看看
        else:
            result.append(i)
    return result


if __name__ == "__main__":
    print(fizzbuzz(15))
