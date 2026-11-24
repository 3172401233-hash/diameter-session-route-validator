import unittest

from diameter_validator import (
    FailureEvent,
    RecoveryEvent,
    RequestEvent,
    ResponseEvent,
    RouteEntry,
    SessionRouteValidator,
)
from diameter_validator.result_codes import (
    DIAMETER_REDIRECT_INDICATION,
    DIAMETER_SUCCESS,
    DIAMETER_UNKNOWN_SESSION_ID,
)
from diameter_validator import validator as v

REALM = "example.com"
APP_ID = 16777251  # Gx


def make_validator():
    entries = [
        RouteEntry(realm=REALM, application_id=APP_ID, node="proxy-a",
                   metric=10),
        RouteEntry(realm=REALM, application_id=APP_ID, node="proxy-b",
                   metric=20),
        RouteEntry(realm=REALM, application_id=None, node="proxy-c",
                   metric=50),
        RouteEntry(realm="*", application_id=None, node="proxy-d",
                   metric=100),
    ]
    nodes = ["proxy-a", "proxy-b", "proxy-c", "proxy-d"]
    return SessionRouteValidator(entries, nodes=nodes)


def initial_request(request_id, session_id, group=None):
    return RequestEvent(
        request_id=request_id,
        origin_host="edge-1.example.com",
        realm=REALM,
        application_id=APP_ID,
        session_id=session_id,
        is_initial=True,
        concurrent_group=group,
    )


def followup_request(request_id, session_id, group=None):
    return RequestEvent(
        request_id=request_id,
        origin_host="edge-1.example.com",
        realm=REALM,
        application_id=APP_ID,
        session_id=session_id,
        is_initial=False,
        concurrent_group=group,
    )


class RouteTableTest(unittest.TestCase):
    def test_overlapping_entries_most_specific_wins(self):
        validator = make_validator()
        result = validator.apply(RequestEvent(
            request_id="r1", origin_host="edge-1", realm=REALM,
            application_id=APP_ID))
        self.assertFalse(result.stateful)
        self.assertEqual(result.target_node, "proxy-a")

    def test_wildcard_realm_fallback(self):
        validator = make_validator()
        result = validator.apply(RequestEvent(
            request_id="r1", origin_host="edge-1", realm="other.net",
            application_id=9999))
        self.assertEqual(result.target_node, "proxy-d")

    def test_realm_only_entry_when_app_unknown(self):
        validator = make_validator()
        result = validator.apply(RequestEvent(
            request_id="r1", origin_host="edge-1", realm=REALM,
            application_id=12345))
        self.assertEqual(result.target_node, "proxy-c")

    def test_failed_node_skipped(self):
        validator = make_validator()
        validator.apply(FailureEvent("proxy-a"))
        result = validator.apply(RequestEvent(
            request_id="r1", origin_host="edge-1", realm=REALM,
            application_id=APP_ID))
        self.assertEqual(result.target_node, "proxy-b")


class PrimaryProxyFailureTest(unittest.TestCase):
    def test_failover_rebinds_and_marks_state_for_rebuild(self):
        validator = make_validator()
        r1 = validator.apply(initial_request("r1", "sess-1"))
        self.assertEqual(r1.target_node, "proxy-a")
        self.assertEqual(r1.conflicts, [])

        validator.apply(FailureEvent("proxy-a"))

        r2 = validator.apply(followup_request("r2", "sess-1"))
        self.assertEqual(r2.target_node, "proxy-b")
        self.assertIn(v.SESSION_STATE_MISSING, r2.conflicts)
        self.assertIn(v.SESSION_DRIFT, r2.conflicts)
        self.assertIn("sess-1", r2.state_to_rebuild)
        self.assertEqual(validator.state_to_rebuild, ["sess-1"])
        self.assertEqual(validator.bindings["sess-1"].node, "proxy-b")

    def test_late_response_from_old_proxy(self):
        validator = make_validator()
        validator.apply(initial_request("r1", "sess-1"))
        validator.apply(FailureEvent("proxy-a"))
        validator.apply(followup_request("r2", "sess-1"))
        validator.apply(RecoveryEvent("proxy-a"))

        validator.apply(ResponseEvent(
            request_id="r2", node="proxy-a",
            result_code=DIAMETER_SUCCESS, session_id="sess-1"))
        r2 = validator.results[-1]
        self.assertIn(v.STALE_PROXY_RESPONSE, r2.conflicts)

    def test_retransmission_after_failure_moves_to_backup(self):
        validator = make_validator()
        validator.apply(initial_request("r1", "sess-1"))
        validator.apply(FailureEvent("proxy-a"))
        retry = validator.apply(RequestEvent(
            request_id="r1-retry", origin_host="edge-1", realm=REALM,
            application_id=APP_ID, session_id="sess-1",
            retransmission_of="r1"))
        self.assertEqual(retry.target_node, "proxy-b")
        self.assertIn("sess-1", retry.state_to_rebuild)


class SessionDriftTest(unittest.TestCase):
    def test_session_id_bound_to_old_node_arrives_without_state(self):
        # 新代理视角：收到携带 Session-Id 的非初始请求，但本地无绑定。
        validator = make_validator()
        result = validator.apply(followup_request("r9", "sess-ghost"))
        self.assertIn(v.SESSION_STATE_MISSING, result.conflicts)
        self.assertIn(v.UNKNOWN_SESSION, result.conflicts)
        self.assertEqual(result.target_node, "proxy-a")
        self.assertIn("sess-ghost", validator.state_to_rebuild)

    def test_unknown_session_id_result_code_drops_binding(self):
        validator = make_validator()
        validator.apply(initial_request("r1", "sess-1"))
        validator.apply(ResponseEvent(
            request_id="r1", node="proxy-a",
            result_code=DIAMETER_UNKNOWN_SESSION_ID,
            session_id="sess-1"))
        self.assertNotIn("sess-1", validator.bindings)
        r1 = validator.results[-1]
        self.assertIn(v.SESSION_STATE_MISSING, r1.conflicts)


class ConcurrentUpdateTest(unittest.TestCase):
    def test_parallel_requests_same_session_share_target(self):
        validator = make_validator()
        r1 = validator.apply(initial_request("r1", "sess-1", group="g1"))
        r2 = validator.apply(followup_request("r2", "sess-1", group="g1"))
        self.assertEqual(r1.target_node, "proxy-a")
        self.assertEqual(r2.target_node, "proxy-a")
        self.assertEqual(r2.conflicts, [])

    def test_parallel_split_detected_when_routes_diverge(self):
        entries = [
            RouteEntry(realm=REALM, application_id=APP_ID, node="proxy-a",
                       metric=10),
            RouteEntry(realm=REALM, application_id=APP_ID, node="proxy-b",
                       metric=20),
        ]
        validator = SessionRouteValidator(
            entries, nodes=["proxy-a", "proxy-b"])
        validator.apply(initial_request("r1", "sess-1", group="g1"))
        # 主节点在并行窗口内故障，第二个并行请求被路由表解析到备用节点。
        validator.apply(FailureEvent("proxy-a"))
        r2 = validator.apply(followup_request("r2", "sess-1", group="g1"))
        self.assertIn(v.PARALLEL_SESSION_SPLIT, r2.conflicts)
        self.assertEqual(r2.target_node, "proxy-a")
        self.assertEqual(validator.bindings["sess-1"].node, "proxy-a")


class RedirectTest(unittest.TestCase):
    def test_redirect_moves_session_target(self):
        validator = make_validator()
        validator.apply(initial_request("r1", "sess-1"))
        validator.apply(ResponseEvent(
            request_id="r1", node="proxy-a",
            result_code=DIAMETER_REDIRECT_INDICATION,
            session_id="sess-1", redirect_host="proxy-x"))
        r2 = validator.apply(followup_request("r2", "sess-1"))
        self.assertEqual(r2.target_node, "proxy-x")

    def test_redirect_loop_detected(self):
        validator = make_validator()
        validator.apply(initial_request("r1", "sess-1"))
        validator.apply(ResponseEvent(
            request_id="r1", node="proxy-a",
            result_code=DIAMETER_REDIRECT_INDICATION,
            session_id="sess-1", redirect_host="proxy-x"))
        r2 = validator.apply(followup_request("r2", "sess-1"))
        self.assertEqual(r2.target_node, "proxy-x")
        # proxy-x 又把会话重定向回 proxy-a（绑定节点），构成循环。
        validator.apply(ResponseEvent(
            request_id="r2", node="proxy-x",
            result_code=DIAMETER_REDIRECT_INDICATION,
            session_id="sess-1", redirect_host="proxy-a"))
        self.assertIn(v.REDIRECT_LOOP, r2.conflicts)
        self.assertEqual(
            validator.bindings["sess-1"].redirect_chain, ["proxy-x"])

    def test_redirect_to_failed_node_falls_back_to_route_table(self):
        validator = make_validator()
        validator.apply(initial_request("r1", "sess-1"))
        validator.apply(ResponseEvent(
            request_id="r1", node="proxy-a",
            result_code=DIAMETER_REDIRECT_INDICATION,
            session_id="sess-1", redirect_host="proxy-x"))
        validator.apply(FailureEvent("proxy-x"))
        r2 = validator.apply(followup_request("r2", "sess-1"))
        self.assertIn(v.REDIRECT_TARGET_DOWN, r2.conflicts)
        self.assertEqual(r2.target_node, "proxy-a")


class StatelessTest(unittest.TestCase):
    def test_stateless_request_creates_no_binding(self):
        validator = make_validator()
        result = validator.apply(RequestEvent(
            request_id="r1", origin_host="edge-1", realm=REALM,
            application_id=APP_ID))
        self.assertFalse(result.stateful)
        self.assertEqual(result.target_node, "proxy-a")
        self.assertEqual(validator.bindings, {})

    def test_no_route_available(self):
        validator = SessionRouteValidator([], nodes=[])
        result = validator.apply(RequestEvent(
            request_id="r1", origin_host="edge-1", realm=REALM,
            application_id=APP_ID))
        self.assertIsNone(result.target_node)
        self.assertIn(v.NO_ROUTE, result.conflicts)


if __name__ == "__main__":
    unittest.main()
