# diameter-session-route-validator

验证 Diameter 会话、路由表和失败切换一致性。

本工具是**纯离线计算器**：它不发送、接收或解析任何真实
Diameter 报文，也不建立 SCTP/TCP 连接。输入是结构化事件
（请求、响应、故障、恢复），输出是每个请求应到达的节点、
会话冲突和需要重建的会话状态。

## 环境

- Python 3.8.2+（仅标准库，无第三方依赖）

## 功能

- 区分无状态请求（无 Session-Id）与有状态会话（粘性绑定）
- 路由表重叠匹配：Realm + Application-ID 最长匹配，metric
  与节点名做确定性平局裁决，自动跳过故障节点
- Result-Code 3006（DIAMETER_REDIRECT_INDICATION）重定向跟踪
  与重定向循环检测
- 同一 Session-Id 的并行请求（concurrent_group）强制落到同一
  节点，路由分叉时报告 PARALLEL_SESSION_SPLIT
- 主代理故障后会话漂移（SESSION_DRIFT）与状态重建清单
- 旧代理晚返回（STALE_PROXY_RESPONSE）与重传去重

## 用法

```python
from diameter_validator import (
    FailureEvent, RequestEvent, RouteEntry, SessionRouteValidator,
)

validator = SessionRouteValidator([
    RouteEntry(realm="example.com", application_id=16777251,
               node="proxy-a", metric=10),
    RouteEntry(realm="example.com", application_id=16777251,
               node="proxy-b", metric=20),
], nodes=["proxy-a", "proxy-b"])

validator.apply(RequestEvent(
    request_id="r1", origin_host="edge-1", realm="example.com",
    application_id=16777251, session_id="sess-1", is_initial=True))
validator.apply(FailureEvent("proxy-a"))
result = validator.apply(RequestEvent(
    request_id="r2", origin_host="edge-1", realm="example.com",
    application_id=16777251, session_id="sess-1"))

print(result.target_node)       # proxy-b
print(result.conflicts)         # ['SESSION_STATE_MISSING', 'SESSION_DRIFT']
print(validator.state_to_rebuild)  # ['sess-1']
```

## 运行测试

```sh
python -m unittest discover -s tests -v
```

测试覆盖：主代理故障切换、Session-Id 漂移、并发更新、
重定向循环、旧代理晚返回、重传、路由表重叠与无状态请求。
