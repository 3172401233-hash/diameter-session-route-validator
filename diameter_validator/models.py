"""数据模型：路由表项、会话绑定与事件。

兼容 Python 3.8.2：仅使用 dataclasses 与 typing 中的泛型。
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class RouteEntry:
    """一条路由表项。

    realm 为 "*" 表示通配所有 Realm；
    application_id 为 None 表示通配所有 Application-ID。
    metric 越小优先级越高；node 用于确定性打破平局。
    """

    realm: str
    application_id: Optional[int]
    node: str
    metric: int = 100

    @property
    def specificity(self) -> int:
        score = 0
        if self.realm != "*":
            score += 2
        if self.application_id is not None:
            score += 1
        return score


@dataclass
class SessionBinding:
    """Session-Id 到节点的粘性绑定。"""

    session_id: str
    node: str
    origin_host: str
    realm: str
    application_id: Optional[int]
    redirect_chain: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class RequestEvent:
    """一个入站 Diameter 请求。

    session_id 为 None 表示无状态请求；
    is_initial 表示会话建立请求（允许尚无绑定）；
    concurrent_group 相同的请求视为并行到达；
    retransmission_of 指向原始 request_id 表示重传。
    """

    request_id: str
    origin_host: str
    realm: str
    application_id: Optional[int] = None
    session_id: Optional[str] = None
    is_initial: bool = False
    concurrent_group: Optional[str] = None
    retransmission_of: Optional[str] = None


@dataclass(frozen=True)
class FailureEvent:
    """代理/节点故障事件。"""

    node: str


@dataclass(frozen=True)
class RecoveryEvent:
    """节点恢复事件（恢复后可能晚返回旧响应）。"""

    node: str


@dataclass(frozen=True)
class ResponseEvent:
    """某节点对先前请求的响应。

    result_code 为 3006 时 redirect_host 给出重定向目标。
    """

    request_id: str
    node: str
    result_code: int
    session_id: Optional[str] = None
    redirect_host: Optional[str] = None


@dataclass
class ValidationResult:
    """单个请求的处理结论。"""

    request_id: str
    target_node: Optional[str]
    stateful: bool
    conflicts: List[str] = field(default_factory=list)
    state_to_rebuild: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
