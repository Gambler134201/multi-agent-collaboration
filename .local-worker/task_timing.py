"""Optional task deadline. Zero/null means no total-runtime cutoff."""
import time


def normalize(value=0):
    if value is None or value == '':
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or (0 < value < 30) or value > 2147483:
        raise ValueError('任务总时限为0表示不限；可选时限需为30到2147483秒的整数')
    return value


def deadline(seconds=0, grace=0):
    seconds = normalize(seconds)
    return time.monotonic() + seconds + grace if seconds else None


def expired(end):
    return end is not None and time.monotonic() > end


def remaining(end):
    return max(1, int(end - time.monotonic())) if end is not None else None
