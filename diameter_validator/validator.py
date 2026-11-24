"""会话路由校验器核心。

按时间顺序消费事件流（请求 / 响应 / 故障 / 恢复），
为每个请求计算目标节点、会话冲突与需要重建的状态。
"""

from typing import Dict, List, Optional, Tuple

from .models import (
    FailureEvent,
    RecoveryEvent,
    RequestEvent,
    ResponseEvent,
    RouteEntry,
    SessionBinding,
    ValidationResult,
)
from .result_codes import (
    DIAMETER_REDIRECT_INDICATION,
    DIAMETER_UNKNOWN_SESSION_ID,
    REDIRECT_RESULT_CODES,
)
from .routing import RouteTable

# 冲突类型常量
SESSION_STATE_MISSING = "SESSION_STATE_MISSING"
SESSION_DRIFT = "SESSION_DRIFT"
PARALLEL_SESSION_SPLIT = "PARALLEL_SESSION_SPLIT"
REDIRECT_LOOP = "REDIRECT_LOOP"
REDIRECT_TARGET_DOWN = "REDIRECT_TARGET_DOWN"
STALE_PROXY_RESPONSE = "STALE_PROXY_RESPONSE"
NO_ROUTE = "NO_ROUTE"
UNKNOWN_SESSION = "UNKNOWN_SESSION"


class SessionRouteValidator:
    """离线校验 Diameter 会话路由一致性。"""  # noqa: E501

    def __init__(self, route_entries: List[RouteEntry],
                 nodes: Optional[List[str]] = None):
        self.route_table = RouteTable(route_entries)
        self.bindings: Dict[str, SessionBinding] = {}
        self.failed_nodes = set()
        self.results: List[ValidationResult] = []
        self._pending: Dict[str, Tuple[RequestEvent, ValidationResult]] = {}
        self._group_targets: Dict[Tuple[str, str], str] = {}
        self._ever_failed = set()
        if nodes:
            self._known_nodes = set(nodes)
        else:
            self._known_nodes = {e.node for e in route_entries}

    # ------------------------------------------------------------------ API
    def apply(self, event) -> Optional[ValidationResult]:
        """消费一个事件；请求事件返回其 ValidationResult。"""
        if isinstance(event, RequestEvent):
            return self._handle_request(event)
        if isinstance(event, ResponseEvent):
            self._handle_response(event)
            return None
        if isinstance(event, FailureEvent):
            self.failed_nodes.add(event.node)
            self._ever_failed.add(event.node)
            return None
        if isinstance(event, RecoveryEvent):
            self.failed_nodes.discard(event.node)
            return None
        raise TypeError("unsupported event: %r" % (event,))

    def apply_all(self, events) -> List[ValidationResult]:
        for event in events:
            self.apply(event)
        return self.results

    @property
    def alive_nodes(self) -> set:
        return self._known_nodes - self.failed_nodes

    @property
    def state_to_rebuild(self) -> List[str]:
        seen = []
        for result in self.results:
            for session_id in result.state_to_rebuild:
                if session_id not in seen:
                    seen.append(session_id)
        return seen

    @property
    def conflicts(self) -> List[Tuple[str, str]]:
        """(request_id, conflict) 列表，按发生顺序。"""
        pairs = []
        for result in self.results:
            for conflict in result.conflicts:
                pairs.append((result.request_id, conflict))
        return pairs

    # ------------------------------------------------------------- requests
    def _handle_request(self, req: RequestEvent) -> ValidationResult:
        if req.retransmission_of is not None:
            result = self._handle_retransmission(req)
        elif req.session_id is None:
            result = self._handle_stateless(req)
        else:
            result = self._handle_stateful(req)
        self.results.append(result)
        self._pending[req.request_id] = (req, result)
        return result

    def _handle_stateless(self, req: RequestEvent) -> ValidationResult:
        target = self.route_table.resolve(
            req.realm, req.application_id, self.alive_nodes)
        result = ValidationResult(
            request_id=req.request_id, target_node=target, stateful=False)
        if target is None:
            result.conflicts.append(NO_ROUTE)
        return result

    def _handle_stateful(self, req: RequestEvent) -> ValidationResult:
        result = ValidationResult(
            request_id=req.request_id, target_node=None, stateful=True)
        binding = self.bindings.get(req.session_id)

        if binding is not None:
            self._route_bound_session(req, binding, result)
        elif req.is_initial:
            target = self._resolve_route(req, result)
            if target is not None:
                self._bind(req, target)
            result.target_node = target
        else:
            # 新代理收到了请求，却没有对应的会话状态。
            result.conflicts.append(SESSION_STATE_MISSING)
            result.conflicts.append(UNKNOWN_SESSION)
            result.state_to_rebuild.append(req.session_id)
            target = self._resolve_route(req, result)
            if target is not None:
                self._bind(req, target)
            result.target_node = target

        self._apply_concurrent_group(req, result)
        return result

    def _route_bound_session(self, req: RequestEvent,
                             binding: SessionBinding,
                             result: ValidationResult) -> None:
        if binding.redirect_chain:
            redirect_target = binding.redirect_chain[-1]
            if redirect_target in self.failed_nodes:
                result.conflicts.append(REDIRECT_TARGET_DOWN)
                result.target_node = self._resolve_route(req, result)
            else:
                result.target_node = redirect_target
            return

        if binding.node not in self.failed_nodes:
            result.target_node = binding.node
            routed = self.route_table.resolve(
                req.realm, req.application_id, self.alive_nodes)
            if routed is not None and routed != binding.node:
                result.notes.append(
                    "sticky: route table prefers %s, session stays on %s"
                    % (routed, binding.node))
            return

        # 绑定节点已故障：会话漂移到新节点，旧状态丢失。
        result.conflicts.append(SESSION_STATE_MISSING)
        result.conflicts.append(SESSION_DRIFT)
        result.state_to_rebuild.append(req.session_id)
        target = self._resolve_route(req, result)
        result.target_node = target
        if target is not None:
            binding.node = target
            binding.redirect_chain = []

    def _apply_concurrent_group(self, req: RequestEvent,
                                result: ValidationResult) -> None:
        if req.concurrent_group is None or req.session_id is None:
            return
        key = (req.concurrent_group, req.session_id)
        established = self._group_targets.get(key)
        if established is None:
            if result.target_node is not None:
                self._group_targets[key] = result.target_node
            return
        if result.target_node != established:
            result.conflicts.append(PARALLEL_SESSION_SPLIT)
            result.notes.append(
                "parallel request pinned to %s (first request in group)"
                % established)
            result.target_node = established
            binding = self.bindings.get(req.session_id)
            if binding is not None:
                binding.node = established

    def _handle_retransmission(self, req: RequestEvent) -> ValidationResult:
        original = self._pending.get(req.retransmission_of)
        result = ValidationResult(
            request_id=req.request_id,
            target_node=None,
            stateful=req.session_id is not None,
        )
        if original is None:
            result.notes.append("retransmission of unknown request")
            result.target_node = self._resolve_route(req, result)
            return result
        original_req, original_result = original
        target = original_result.target_node
        if target is not None and target in self.failed_nodes:
            result.notes.append(
                "original target %s down, retransmitting via route table"
                % target)
            target = self._resolve_route(req, result)
            if req.session_id is not None and target is not None:
                binding = self.bindings.get(req.session_id)
                if binding is not None and binding.node != target:
                    binding.node = target
                    result.state_to_rebuild.append(req.session_id)
                    result.conflicts.append(SESSION_STATE_MISSING)
        else:
            result.notes.append(
                "duplicate of %s, same target" % original_req.request_id)
        result.target_node = target
        return result

    # ------------------------------------------------------------ responses
    def _handle_response(self, resp: ResponseEvent) -> None:
        pending = self._pending.get(resp.request_id)
        result = pending[1] if pending else None

        binding = self.bindings.get(resp.session_id) \
            if resp.session_id else None

        if binding is not None and resp.node != binding.node \
                and resp.node in self._ever_failed:
            # 旧代理晚返回：会话已重新绑定到其他节点。
            if result is not None:
                result.conflicts.append(STALE_PROXY_RESPONSE)
                result.notes.append(
                    "late response from old proxy %s, session now on %s"
                    % (resp.node, binding.node))

        if resp.result_code == DIAMETER_UNKNOWN_SESSION_ID \
                and resp.session_id is not None:
            if result is not None:
                result.conflicts.append(SESSION_STATE_MISSING)
                result.state_to_rebuild.append(resp.session_id)
            self.bindings.pop(resp.session_id, None)
            return

        if resp.result_code in REDIRECT_RESULT_CODES and resp.redirect_host:
            self._apply_redirect(resp, binding, result)

    def _apply_redirect(self, resp: ResponseEvent,
                        binding: Optional[SessionBinding],
                        result: Optional[ValidationResult]) -> None:
        if binding is None:
            if result is not None:
                result.notes.append(
                    "redirect for stateless request to %s"
                    % resp.redirect_host)
            return
        chain = binding.redirect_chain
        if resp.redirect_host in chain \
                or resp.redirect_host == binding.node:
            if result is not None:
                result.conflicts.append(REDIRECT_LOOP)
                result.notes.append(
                    "redirect loop detected at %s" % resp.redirect_host)
            return
        if resp.result_code == DIAMETER_REDIRECT_INDICATION:
            chain.append(resp.redirect_host)
            self._known_nodes.add(resp.redirect_host)
            if result is not None:
                result.notes.append(
                    "session redirected to %s" % resp.redirect_host)

    # -------------------------------------------------------------- helpers
    def _resolve_route(self, req: RequestEvent,
                       result: ValidationResult) -> Optional[str]:
        target = self.route_table.resolve(
            req.realm, req.application_id, self.alive_nodes)
        if target is None and NO_ROUTE not in result.conflicts:
            result.conflicts.append(NO_ROUTE)
        return target

    def _bind(self, req: RequestEvent, node: str) -> None:
        self.bindings[req.session_id] = SessionBinding(
            session_id=req.session_id,
            node=node,
            origin_host=req.origin_host,
            realm=req.realm,
            application_id=req.application_id,
        )
