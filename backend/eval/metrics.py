"""延迟分位数采用排序后线性插值；空样本不报告耗时。"""
import math


def percentile(values, percent):
    if not 0 <= percent <= 100:
        raise ValueError("分位数必须在 0 到 100 之间")
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * percent / 100
    lo, hi = math.floor(position), math.ceil(position)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)
