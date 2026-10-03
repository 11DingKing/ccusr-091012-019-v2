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

## 容器

```bash
docker build -t custody-service .
docker run --rm custody-service
```

## 通知投递箱（冻结 / 驳回 / 放行）

状态变更不再依赖调用方同步发提醒。`POST /api/goods/<id>/custody-status/`
在**同一数据库事务**内写入业务记录（`wh_custody_status`）与通知投递箱
（`wh_notification_outbox`），调用方超时 / 崩溃也不会出现“业务成功却无待通知记录”。

进程内作业（`apps.warehouse.notifications.runner`，随 gunicorn/runserver 启动；
也可用 `python manage.py dispatch_notifications` 手动触发）分批**认领**投递箱记录：

- 状态机：`pending → sending → succeeded`，失败转 `failed` 并按指数退避重试，
  重试耗尽或收件人失效转 `dead`（终败留痕，不再重试、不丢失）；
- 每次尝试写入 `wh_notification_delivery`，成功 / 重试 / 终败全程可查询、可审计；
- 认领带租约（`lease_expires_at`），作业崩溃后租约到期，任意存活作业自动捞回；
- `idempotency_key` 唯一约束挡下重复事件；下游发送器以幂等键去重，租约恢复
  重投不会产生重复通知。

不连接真实外部服务，`SimulatedSender` 为内存模拟发送器，可脚本化临时故障与
收件人失效。投递状态可通过 `GET /api/notifications/`（支持 `status`/`event_type`
等过滤）与 `GET /api/notifications/<id>/` 查询。

相关参数见 `settings.py` 的 `NOTIFICATION_*`（批大小、租约秒数、最大尝试次数、
退避基数 / 上限、后台作业开关）。

