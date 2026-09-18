"""时间与时区工具。

两条约定：

1. **数据库里统一存 UTC**，用户看到和输入的是**本地时间**（中国标准时间 UTC+8）。
2. 所有转换都必须走这个模块，**不要在视图里手写加减 8 小时**。
   漏改一处不会报错、不会崩溃，只会让活动时间显示早 8 小时——
   属于最难发现、后果又最直接（人跑错场地）的一类缺陷。

为什么用固定偏移 ``timedelta(hours=8)`` 而不是 ``ZoneInfo("Asia/Shanghai")``：
中国标准时间全年没有夏令时，固定偏移已经足够；Windows 不自带 IANA 时区库，
用 ``ZoneInfo`` 还得额外装 tzdata，为这一个时区不值得。
"""

import re
from datetime import datetime, timedelta, timezone

# 展示用时区
DISPLAY_TZ = timezone(timedelta(hours=8))

# 匹配 <input type="datetime-local"> 提交的值，例如 2026-09-20T14:30 或 2026-09-20T14:30:00。
# 秒是可选的：不设 step 属性时浏览器不会带秒。
_LOCAL_INPUT_RE = re.compile(
    r"^(\d{4})-(\d{1,2})-(\d{1,2})[T ](\d{1,2}):(\d{2})(?::(\d{2}))?$"
)


def to_local(value):
    """把数据库里的（无时区的）UTC 时间转成本地时间。"""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).astimezone(DISPLAY_TZ)


def to_utc(naive_local):
    """把本地时间转成（无时区的）UTC，用于写库。"""
    if naive_local is None:
        return None
    return naive_local.replace(tzinfo=DISPLAY_TZ).astimezone(timezone.utc).replace(
        tzinfo=None
    )


def parse_local(value):
    """解析用户提交的本地时间字符串，返回可入库的 UTC 时间。

    解析不了就返回 ``None``（由调用方转成表单错误），**不要抛异常**——
    用户手输或改表单参数都可能送进来各种奇怪的值。
    """
    if not value:
        return None
    match = _LOCAL_INPUT_RE.match(str(value).strip())
    if match is None:
        return None

    numbers = [int(part) for part in match.groups() if part is not None]
    try:
        naive_local = datetime(*numbers)
    except ValueError:
        # 比如 2026-02-30 这种日历上不存在的日期
        return None
    return to_utc(naive_local)


def format_local(value, fmt="%m-%d %H:%M"):
    """本地时间的格式化，``None`` 显示成破折号。"""
    local = to_local(value)
    if local is None:
        return "—"
    return local.strftime(fmt)


def format_local_input(value):
    """格式化成 ``<input type="datetime-local">`` 能回填的形式。"""
    return format_local(value, "%Y-%m-%dT%H:%M")


def utc_iso(value):
    """给 CSV 之类的地方用：转成本地时间的可读字符串。"""
    local = to_local(value)
    if local is None:
        return ""
    return local.strftime("%Y-%m-%d %H:%M")
