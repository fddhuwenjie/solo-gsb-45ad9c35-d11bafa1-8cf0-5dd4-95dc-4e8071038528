"""URL 路径解码与规范化。

审计器在一个明确定义的规范化请求路径上模拟路由器匹配，规则如下：

1. 仅对 ``%20`` 这类百分号转义做安全解码：
   - ``%2F``、``%3F``、``%23`` 等保留字符（``:/?#[]@!$&'()*+,;=``）**不**解码，
     因此编码斜杠不会变成段分隔符；
   - 非保留字符（RFC 3986 unreserved：A-Za-z0-9-._~）被解码；
   - 其余字节保持百分号形式，并把十六进制位规范为大写（``%2f`` -> ``%2F``）；
   - 非法转义（``%`` 后不是两位十六进制）或 ASCII 控制字符报错。
2. 按 RFC 3986 §5.2.4 移除 ``.`` / ``..`` 点段（``..`` 不可越过根 ``/``）。
3. 无尾随斜杠语义：结尾空段（尾随 ``/``）被移除，``/`` 归一化为根。
4. 内部空段（``//``）保留——它是合法但无法被静态段/参数段匹配的段。
"""

from __future__ import annotations

RESERVED = set(":/?#[]@!$&'()*+,;=")
_UNRESERVED = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_HEX = set("0123456789abcdefABCDEF")
_CONTROL_MAX = 0x1F


class PathError(ValueError):
    """路径包含非法百分号转义或控制字符。"""


def _decode(raw_segments: list[str]) -> list[str]:
    """逐段做安全百分号解码，保留 ``/`` 分隔。"""

    def decode_segment(segment: str) -> str:
        for ch in segment:
            if ch != "%" and (ord(ch) <= _CONTROL_MAX or ord(ch) == 0x7F):
                raise PathError("path contains ASCII control character")
        if "%" not in segment:
            return segment
        out: list[str] = []
        chars = list(segment)
        i = 0
        while i < len(chars):
            ch = chars[i]
            if ch != "%":
                if ord(ch) <= _CONTROL_MAX or ord(ch) == 0x7F:
                    raise PathError("path contains ASCII control character")
                out.append(ch)
                i += 1
                continue
            if i + 2 >= len(chars) or chars[i + 1] not in _HEX or chars[i + 2] not in _HEX:
                raise PathError(f"malformed percent-escape near {segment[i:i+3]!r}")
            value = int(chars[i + 1] + chars[i + 2], 16)
            if value <= _CONTROL_MAX or value == 0x7F:
                raise PathError("percent-encoded control character is not allowed")
            decoded = chr(value)
            if decoded in RESERVED:
                # 保留字符不解码（含 %2F 斜杠），但统一大写十六进制。
                out.append("%" + chars[i + 1].upper() + chars[i + 2].upper())
            elif decoded in _UNRESERVED:
                out.append(decoded)
            else:
                # 其它字节（如 UTF-8 续字节）保留转义形式，统一大写。
                out.append("%" + chars[i + 1].upper() + chars[i + 2].upper())
            i += 3
        return "".join(out)

    return [decode_segment(seg) for seg in raw_segments]


def _remove_dot_segments(segments: list[str]) -> list[str]:
    """RFC 3986 §5.2.4 算法 B 的等价实现（输入已按 ``/`` 切分）。"""
    stack: list[str] = []
    for seg in segments:
        if seg == ".":
            continue
        if seg == "..":
            if stack:
                stack.pop()
            continue
        stack.append(seg)
    return stack


def split_path(path: str) -> list[str]:
    """把绝对路径按 ``/`` 切成原始段（不做任何解码）。"""
    return path.split("/")


def normalize_segments(raw_segments: list[str]) -> list[str]:
    """对已切分的原始段执行解码、点段移除和尾随空段移除。"""
    decoded = _decode(raw_segments)
    cleaned = _remove_dot_segments(decoded)
    # 尾随 "/" 产生的空段全部移除（根路径因此变为 []）。
    while cleaned and cleaned[-1] == "":
        cleaned.pop()
    return cleaned


def normalize_path(path: str) -> list[str]:
    """规范化完整绝对路径，返回段列表（空列表表示根 ``/``）。

    绝对路径 ``split('/')`` 得到的首空段是根标记，解码检查后移除。
    """
    raw = split_path(path)
    decoded = _decode(raw)  # 含首空段的解码合法性检查
    if decoded[:1] == [""]:
        decoded = decoded[1:]
    cleaned = _remove_dot_segments(decoded)
    while cleaned and cleaned[-1] == "":
        cleaned.pop()
    return cleaned


def render(segments: list[str]) -> str:
    """把段列表渲染为路径；空列表渲染为根 ``/``。"""
    if not segments:
        return "/"
    return "/" + "/".join(segments)
