"""报名请求的轻量运行时指标（DR-5）。

《实验3-需求分析》指出：报名开放瞬间是性能瓶颈（P95 1555ms、15.6% 请求 >1s），
要求「监控看板可观测峰值指标」。这里是这个要求的最小落地：

* 每次报名请求结束时记录一次耗时（环形缓冲，保留最近 500 条）；
* 管理端「运行指标」页展示 P50 / P95 / P99、慢请求占比、错误数；
* **纯内存**：进程重启即清零。这是内存统计的固有局限，页面上会写明。
  不落库是有意的——指标是观测用途，不该占用业务表，也不该被
  「每次启动重播演示数据」反复写入。
"""

import math
from collections import deque
from threading import Lock

# 保留最近多少次报名请求的样本
MAX_SAMPLES = 500

# 「慢请求」阈值：与需求分析附录 B 的口径一致（latency_ms > 300）
SLOW_MS = 300

_samples = deque(maxlen=MAX_SAMPLES)
_lock = Lock()
_errors = 0


def record(elapsed_ms, ok=True):
    """记录一次报名请求。``ok=False`` 表示业务上失败（仍计入耗时与慢请求）。"""
    global _errors
    with _lock:
        _samples.append({"ms": round(float(elapsed_ms), 1), "ok": bool(ok)})
        if not ok:
            _errors += 1


def reset():
    """清空统计（测试用）。"""
    global _errors
    with _lock:
        _samples.clear()
        _errors = 0


def _percentile(values, p):
    """线性插值分位数。values 必须已排序。"""
    if not values:
        return None
    k = (len(values) - 1) * p
    low, high = math.floor(k), math.ceil(k)
    if low == high:
        return values[int(k)]
    return values[low] + (values[high] - values[low]) * (k - low)


def snapshot():
    """当前指标快照。没有样本时各分位数返回 ``None``。"""
    with _lock:
        values = sorted(s["ms"] for s in _samples)
        total = len(values)
        errors = _errors

    slow = sum(1 for v in values if v > SLOW_MS)
    return {
        "total": total,
        "errors": errors,
        "p50": _percentile(values, 0.50),
        "p90": _percentile(values, 0.90),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "max": values[-1] if values else None,
        "slow": slow,
        "slow_ratio": (slow / total) if total else None,
        "slow_ms": SLOW_MS,
        "window": MAX_SAMPLES,
    }


__all__ = ["record", "reset", "snapshot", "MAX_SAMPLES", "SLOW_MS"]
