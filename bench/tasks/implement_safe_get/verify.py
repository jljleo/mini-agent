import sys

sys.path.insert(0, ".")

from safe_get import safe_get

TEST_CASES = [
    ({"a": {"b": 1}}, "a.b", None, 1),
    ({"a": [{"b": 2}]}, "a.0.b", None, 2),
    ({"a": {"b": {"c": 3}}}, "a.b.c", None, 3),
    ({}, "a.b", "default", "default"),
    ({"a": [1, 2]}, "a.5", -1, -1),
    (None, "a", "x", "x"),
    ({"a": {"b": "value"}}, "a.b.c", None, None),
    ({"x": {"y": {"z": 42}}}, "x.y.z", 0, 42),
]


def main():
    passed = 0
    for data, path, default, expected in TEST_CASES:
        try:
            got = safe_get(data, path, default)
            if got == expected:
                passed += 1
        except Exception:
            pass

    score = passed / len(TEST_CASES)
    print(f"score={score:.2f}")
    sys.exit(0 if score >= 0.5 else 1)


if __name__ == "__main__":
    main()
