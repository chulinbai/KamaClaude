from __future__ import annotations


# 将外部步数设置规范化为正整数或不限，拒绝布尔值和隐式类型转换
def normalize_max_steps(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError("max_steps must be a non-negative integer (0 means unlimited)")
    return value or None
