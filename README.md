# 监管物资保管服务

该项目为监管仓、证物室和受控物资保管点提供服务端 API，覆盖人员授权、物资分类、批次登记、收发记录、审批、预警、审计日志与统计报表。数据保存在 SQLite，所有测试和接口验收均可在单个 Linux 应用容器内离线完成。

## 运行环境

- Python 3.11
- Django REST Framework
- SQLite

## 安装与初始化

```bash
python -m pip install -r backend/requirements.txt
cd backend
python manage.py migrate --run-syncdb
```

## 测试

```bash
cd backend
pytest -q
```

## 编译检查

```bash
python -m compileall -q backend
```

## API 验收

```bash
cd backend
python manage.py migrate --run-syncdb
python manage.py shell -c "from rest_framework.test import APIClient; from apps.authentication.models import User; u=User.objects.create_user('smoke','safe-pass',role='admin'); c=APIClient(); r=c.post('/api/auth/login/',{'username':'smoke','password':'safe-pass'},format='json'); print(r.status_code, bool(r.json()['data']['token']))"
```

## 通知投递箱

监管物资（冻结/驳回/放行）状态变化时，状态日志、物资状态与通知投递箱记录在**同一数据库事务**中写入，调用方超时重试也不会出现"业务成功却无待通知记录"。

投递由进程内作业分批认领完成，无需真实外部服务：

```bash
cd backend
python manage.py process_notifications --once      # 处理一批
python manage.py process_notifications              # 常驻循环
# 或设置 NOTIFICATION_WORKER_ENABLED=true，由应用进程启动守护线程
```

关键保证：

- **重复事件**：状态变更接口接受 `idempotency_key`；投递箱 `(event_type, dedup_key)` 唯一约束兜底，同一事件至多一条记录、通知只发一次。
- **租约恢复**：每行带 `lease_owner/lease_expires_at`；作业崩溃遗留的 `processing` 记录在租约到期后由后续批次重新认领，`delivered_recipients` 保证已成功收件人不会收到第二次。
- **退避重试**：临时失败按 `NOTIFICATION_BACKOFF_BASE_SECONDS` 起指数退避（30s→60s→…，封顶 30min），达到 `NOTIFICATION_MAX_ATTEMPTS`（默认 5）后置为 `failed` 死信，不再被认领。
- **收件人失效**：模拟发送器返回失效收件人后，该收件人从待发列表永久剔除并记入流水，其余收件人正常送达，不产生无限重试。

投递状态与每轮流水（成功/重试/死信）均可通过 API 查询：

```
GET /api/notifications/outbox/?status=pending|processing|succeeded|failed
GET /api/notifications/outbox/<id>/
GET /api/custody-goods/<id>/status-logs/
```

模拟发送器 `apps.warehouse.notifications.senders.MockSender` 默认全部成功，可通过类方法注入故障（`fail_times` / `fail_once` / `mark_invalid`），发送记录可查询；接入真实服务时实现 `BaseSender.send` 并通过 `NOTIFICATION_SENDER` 配置切换。

## 容器

```bash
docker build -t custody-service .
docker run --rm custody-service
```
