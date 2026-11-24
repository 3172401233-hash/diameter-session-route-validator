"""路由表解析：处理 Realm / Application-ID 重叠匹配。"""

from typing import List, Optional

from .models import RouteEntry


class RouteTable:
    """按最长匹配（specificity）+ metric + 节点名解析路由。"""  # noqa: E501

    def __init__(self, entries: List[RouteEntry]):
        self._entries = list(entries)

    def add(self, entry: RouteEntry) -> None:
        self._entries.append(entry)

    def remove(self, entry: RouteEntry) -> None:
        self._entries.remove(entry)

    def _matches(self, entry: RouteEntry, realm: str,
                 application_id: Optional[int]) -> bool:
        if entry.realm != "*" and entry.realm != realm:
            return False
        if entry.application_id is not None \
                and entry.application_id != application_id:
            return False
        return True

    def resolve(self, realm: str, application_id: Optional[int],
                alive_nodes: Optional[set] = None) -> Optional[str]:
        """返回应处理该请求的节点；无可用路由时返回 None。"""
        candidates = [
            e for e in self._entries if self._matches(e, realm, application_id)
        ]
        if alive_nodes is not None:
            candidates = [e for e in candidates if e.node in alive_nodes]
        if not candidates:
            return None
        best = min(
            candidates,
            key=lambda e: (-e.specificity, e.metric, e.node),
        )
        return best.node
