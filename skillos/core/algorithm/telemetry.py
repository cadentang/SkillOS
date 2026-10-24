"""token 归属（N-11.3）：**估算**口径，纯函数。

一次会话常有多 skill 参与，token 无法精确归属单个 skill（Runtime 不提供 per-call 计量）。
本模块实现规格里唯一的定义：

> 会话内某 skill 调用事件**之后**的 usage 片段归该 skill，直到下一个**不同 skill**的调用事件；
> 无任何 skill 调用的会话 usage 归 `unattributed`。

UI 必须标注「估算口径」（N-11.3 末句），且**禁止**用于计费/考核类精确决策 ——
所以本模块只输出数字与归属，不提供任何「精确」措辞或四舍五入后的伪精确值。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

# 未归属桶的名字（N-11.3 明文规定，不是随便起的占位符）
UNATTRIBUTED = "unattributed"


@dataclass(frozen=True, order=True)
class CallEvent:
    """会话内的一次 skill 调用（由适配器从会话日志里命中部署路径得出）。"""

    at: datetime
    skill_id: str


@dataclass(frozen=True, order=True)
class UsageSlice:
    """会话内的一段 usage（Claude Code 按 assistant 消息逐条给）。"""

    at: datetime
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0


@dataclass
class TokenTotals:
    """某 skill 的 token 合计（三列与 Runtime 提供的 usage 字段一一对应）。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0

    def add(self, slice_: UsageSlice) -> None:
        self.input_tokens += slice_.input_tokens
        self.output_tokens += slice_.output_tokens
        self.cache_read += slice_.cache_read

    def merge(self, other: TokenTotals) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read += other.cache_read

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read

    def as_dict(self) -> dict[str, int]:
        return {
            "tokens_in": self.input_tokens,
            "tokens_out": self.output_tokens,
            "cache_read": self.cache_read,
        }


@dataclass
class Attribution:
    """归属结果：按 skill 汇总 + 未归属桶。"""

    by_skill: dict[str, TokenTotals] = field(default_factory=dict)
    unattributed: TokenTotals = field(default_factory=TokenTotals)


def attribute_tokens(
    events: Iterable[CallEvent],
    slices: Iterable[UsageSlice],
) -> Attribution:
    """按时间片把 usage 归到「最近一次调用」的 skill（N-11.3）。

    - 时间相同：调用事件先于 usage 片段（`<=`），否则同毫秒的调用会被自己那条 usage 错过；
    - 首个调用事件之前的 usage → `unattributed`；
    - 最后一次调用之后的 usage 仍归该 skill（会话尾部没有新的调用，但 token 确实花了）；
    - 排序稳定，输入顺序不影响结果（同一批日志重算必须得出同一份数字）。
    """
    ordered_events = sorted(events)          # CallEvent 的 order=True 按 (at, skill_id)
    ordered_slices = sorted(slices)

    result = Attribution()
    cursor = 0
    current: str | None = None
    for slice_ in ordered_slices:
        while cursor < len(ordered_events) and ordered_events[cursor].at <= slice_.at:
            current = ordered_events[cursor].skill_id
            cursor += 1
        if current is None:
            result.unattributed.add(slice_)
            continue
        bucket = result.by_skill.get(current)
        if bucket is None:
            bucket = TokenTotals()
            result.by_skill[current] = bucket
        bucket.add(slice_)
    return result
