def binary_search(arr, target):
    """在有序数组 arr 中二分查找 target，返回下标；不存在返回 -1。"""
    lo, hi = 0, len(arr) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            hi = mid - 1  # FIXME: 这里分支方向好像反了
        else:
            lo = mid + 1  # FIXME: 这里也看看
    return -1


if __name__ == "__main__":
    print(binary_search([1, 3, 5, 7, 9], 5))
