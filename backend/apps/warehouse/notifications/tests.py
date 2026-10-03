"""
通知投递箱验收测试：

- 状态变化与投递箱记录同事务提交/回滚；
- 重复事件（幂等键 / dedup_key 唯一约束）不产生重复通知；
- 作业崩溃后租约到期可恢复，且不会对已成功收件人重复发送；
- 临时失败指数退避重试，超过上限进入死信，不无限重复；
- 失效收件人被永久剔除，不阻塞其他收件人也不触发无限重试；
- 投递状态与每轮流水可通过 API 查询。
"""
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from ..models import (
    CustodyGoods,
    CustodyStatusLog,
    NotificationDelivery,
    NotificationOutbox,
)
from .outbox import (
    change_custody_status,
    claim_batch,
    custody_dedup_key,
    enqueue_event,
    run_delivery_batch,
)
from .senders import MockSender, TransientSendError
from .worker import run_worker


@override_settings(
    NOTIFICATION_BACKOFF_BASE_SECONDS=0,
    NOTIFICATION_BACKOFF_CAP_SECONDS=0,
    NOTIFICATION_LEASE_SECONDS=60,
)
class NotificationOutboxTest(TestCase):
    def setUp(self):
        MockSender.reset()
        self.user = User.objects.create_user('notify-admin', 'testpass123', role='admin')
        self.goods = CustodyGoods.objects.create(
            name='涉案手机', code='EV-1001', registered_by=self.user,
            status=CustodyGoods.STATUS_FROZEN,
        )
        self.recipients = ['admin-a', 'admin-b']

    def _transition(self, to_status=CustodyGoods.STATUS_RELEASED, **kwargs):
        return change_custody_status(
            self.goods, to_status, self.user,
            recipients=self.recipients, **kwargs
        )

    # ---------- 同事务原子性 ----------

    def test_transition_writes_outbox_in_same_transaction(self):
        goods, log, outbox, duplicate = self._transition(remark='准予放行')
        self.assertFalse(duplicate)
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.status, CustodyGoods.STATUS_RELEASED)
        self.assertEqual(CustodyStatusLog.objects.count(), 1)
        self.assertEqual(outbox.status, NotificationOutbox.STATUS_PENDING)
        self.assertEqual(outbox.recipients, self.recipients)
        self.assertEqual(outbox.payload['to_status'], 'released')
        self.assertEqual(outbox.dedup_key, custody_dedup_key(self.goods.id, log.id))

    def test_business_rolls_back_when_outbox_write_fails(self):
        with mock.patch(
            'apps.warehouse.notifications.outbox.NotificationOutbox.objects'
        ) as manager_mock:
            manager_mock.create.side_effect = RuntimeError('db down')
            manager_mock.filter.return_value.first.return_value = None
            with self.assertRaises(RuntimeError):
                self._transition()

        self.goods.refresh_from_db()
        self.assertEqual(self.goods.status, CustodyGoods.STATUS_FROZEN)
        self.assertEqual(CustodyStatusLog.objects.count(), 0)
        self.assertEqual(NotificationOutbox.objects.count(), 0)

    # ---------- 重复事件 ----------

    def test_same_idempotency_key_is_a_noop(self):
        _, log1, outbox1, dup1 = self._transition(idempotency_key='evt-001')
        self.assertFalse(dup1)
        # 相同请求体重试（调用方超时后重发）也必须幂等，不能报“已是该状态”
        goods_same, log_same, outbox_same, dup_same = self._transition(
            to_status=CustodyGoods.STATUS_RELEASED, idempotency_key='evt-001'
        )
        self.assertTrue(dup_same)
        self.assertEqual(log_same.id, log1.id)
        # 不同状态的同键请求同样被忽略
        goods2, log2, outbox2, dup2 = self._transition(
            to_status=CustodyGoods.STATUS_REJECTED, idempotency_key='evt-001'
        )
        self.assertTrue(dup2)
        self.assertEqual(log2.id, log1.id)
        self.assertEqual(outbox2.id, outbox1.id)
        self.assertEqual(CustodyStatusLog.objects.count(), 1)
        self.assertEqual(NotificationOutbox.objects.count(), 1)
        # 第二次提交的目标状态被忽略，物资保持首次的放行
        self.assertEqual(goods2.status, CustodyGoods.STATUS_RELEASED)

    def test_enqueue_same_dedup_key_returns_existing_row(self):
        first = enqueue_event(
            NotificationOutbox.EVENT_CUSTODY_STATUS_CHANGED,
            'dup-1', {'x': 1}, ['r1'],
        )
        second = enqueue_event(
            NotificationOutbox.EVENT_CUSTODY_STATUS_CHANGED,
            'dup-1', {'x': 2}, ['r2'],
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(NotificationOutbox.objects.count(), 1)

    # ---------- 成功投递 ----------

    def test_worker_delivers_and_records_success(self):
        _, log, outbox, _ = self._transition()
        stats = run_worker(max_loops=1, interval_seconds=0)

        self.assertEqual(stats['claimed'], 1)
        self.assertEqual(stats['succeeded'], 1)
        outbox.refresh_from_db()
        self.assertEqual(outbox.status, NotificationOutbox.STATUS_SUCCEEDED)
        self.assertEqual(sorted(outbox.delivered_recipients), self.recipients)
        self.assertEqual(outbox.attempts, 1)
        self.assertIsNotNone(outbox.succeeded_at)

        delivery = outbox.deliveries.get()
        self.assertEqual(delivery.result, NotificationDelivery.RESULT_SUCCESS)
        self.assertEqual(delivery.success_count, 2)

        self.assertEqual(len(MockSender.sent), 1)
        self.assertEqual(MockSender.sent[0]['dedup_key'], outbox.dedup_key)
        self.assertEqual(sorted(MockSender.sent[0]['recipients']), self.recipients)

        # 终态记录不会再被认领
        self.assertEqual(claim_batch(), [])

    # ---------- 退避重试与死信 ----------

    def test_transient_failure_retries_with_backoff_then_succeeds(self):
        _, _, outbox, _ = self._transition()
        MockSender.fail_once(outbox.dedup_key, message='gateway timeout')

        stats = run_worker(max_loops=2, interval_seconds=0)
        self.assertEqual((stats['retry'], stats['succeeded']), (1, 1))

        outbox.refresh_from_db()
        self.assertEqual(outbox.status, NotificationOutbox.STATUS_SUCCEEDED)
        self.assertEqual(outbox.attempts, 2)
        self.assertIn('gateway timeout', outbox.last_error)
        results = list(outbox.deliveries.order_by('id').values_list('result', flat=True))
        self.assertEqual(results, [
            NotificationDelivery.RESULT_RETRY,
            NotificationDelivery.RESULT_SUCCESS,
        ])
        # 重试期间没有产生任何成功发送记录
        self.assertEqual(len(MockSender.sent), 1)

    def test_exhausting_attempts_marks_dead_and_stops(self):
        _, _, outbox, _ = self._transition()
        outbox.max_attempts = 3
        outbox.save()
        MockSender.fail_times(10, message='persistent outage')

        stats = run_worker(max_loops=4, interval_seconds=0)
        self.assertEqual(stats['dead'], 1)
        self.assertEqual(stats['succeeded'], 0)

        outbox.refresh_from_db()
        self.assertEqual(outbox.status, NotificationOutbox.STATUS_FAILED)
        self.assertEqual(outbox.attempts, 3)
        results = list(outbox.deliveries.order_by('id').values_list('result', flat=True))
        self.assertEqual(results, [
            NotificationDelivery.RESULT_RETRY,
            NotificationDelivery.RESULT_RETRY,
            NotificationDelivery.RESULT_DEAD,
        ])
        self.assertEqual(MockSender.sent, [])
        # 死信不再被认领，不会无限重复
        self.assertEqual(claim_batch(), [])

    def test_failed_row_not_claimable_before_backoff_window(self):
        _, _, outbox, _ = self._transition()
        outbox.max_attempts = 3
        outbox.save()
        MockSender.fail_times(10)
        # 用正的退避时间验证退避窗口（base 与 cap 都要覆盖类级设置）
        with override_settings(
            NOTIFICATION_BACKOFF_BASE_SECONDS=300,
            NOTIFICATION_BACKOFF_CAP_SECONDS=3600,
        ):
            run_delivery_batch()
        outbox.refresh_from_db()
        self.assertEqual(outbox.status, NotificationOutbox.STATUS_PENDING)
        self.assertGreater(outbox.available_at, timezone.now())
        self.assertEqual(claim_batch(), [])

    # ---------- 租约恢复 ----------

    def test_concurrent_claim_is_exclusive(self):
        # 直接登记 3 条待投递记录
        for i in range(3):
            enqueue_event(
                NotificationOutbox.EVENT_CUSTODY_STATUS_CHANGED,
                f'c-{i}', {'i': i}, ['r1'],
            )

        now = timezone.now()
        first = claim_batch(owner='worker-A', now=now)
        self.assertEqual(len(first), 3)
        # 第二个实例在同一时刻用相同条件再次认领：UPDATE 的 WHERE 重验失败，抢不到
        second = claim_batch(owner='worker-B', now=now)
        self.assertEqual(second, [])
        self.assertEqual(
            NotificationOutbox.objects.filter(lease_owner='worker-A').count(), 3
        )

    def test_expired_lease_is_reclaimed_without_duplicate_send(self):
        _, _, outbox, _ = self._transition()

        # 作业实例 A 认领后崩溃：记录滞留 processing，发送器崩溃前
        # 已对 admin-a 发过但未来得及记账成功状态
        claim_batch(owner='worker-A')
        NotificationOutbox.objects.filter(pk=outbox.pk).update(
            status=NotificationOutbox.STATUS_PROCESSING,
            lease_owner='worker-A',
            lease_expires_at=timezone.now() + timedelta(seconds=60),
            attempts=1,
            delivered_recipients=['admin-a'],
        )

        # 租约未过期时其他实例不能抢
        self.assertEqual(claim_batch(owner='worker-B'), [])

        # 租约到期（作业崩溃未续约）后，实例 B 可以恢复投递
        NotificationOutbox.objects.filter(pk=outbox.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )
        stats = run_worker(max_loops=1, interval_seconds=0, owner='worker-B')
        self.assertEqual(stats['succeeded'], 1)

        outbox.refresh_from_db()
        self.assertEqual(outbox.status, NotificationOutbox.STATUS_SUCCEEDED)
        self.assertEqual(outbox.lease_owner, '')
        self.assertEqual(sorted(outbox.delivered_recipients), self.recipients)
        sent_to = MockSender.sent[0]['recipients']
        self.assertEqual(sent_to, ['admin-b'])  # admin-a 不会被重复发送
        self.assertNotIn('admin-a', sent_to)

    # ---------- 失效收件人 ----------

    def test_invalid_recipient_is_removed_and_not_retried(self):
        recipients = ['gone-user', 'active-user']
        change_custody_status(
            self.goods, CustodyGoods.STATUS_REJECTED, self.user,
            recipients=recipients,
        )
        MockSender.mark_invalid(['gone-user'])

        stats = run_worker(max_loops=2, interval_seconds=0)
        self.assertEqual(stats['succeeded'], 1)
        self.assertEqual(stats['retry'], 0)

        outbox = NotificationOutbox.objects.get()
        self.assertEqual(outbox.status, NotificationOutbox.STATUS_SUCCEEDED)
        self.assertEqual(outbox.delivered_recipients, ['active-user'])
        self.assertEqual(outbox.recipients, [])
        delivery = outbox.deliveries.get()
        self.assertEqual(delivery.invalid_recipients, ['gone-user'])
        self.assertEqual(delivery.success_count, 1)
        self.assertEqual(MockSender.sent[0]['recipients'], ['active-user'])


class CustodyNotificationAPITest(TestCase):
    def setUp(self):
        MockSender.reset()
        self.user = User.objects.create_user('api-admin', 'testpass123', role='admin')
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.user)}")
        self.goods = CustodyGoods.objects.create(
            name='封存硬盘', code='EV-2002', registered_by=self.user,
        )

    def test_transition_endpoint_and_outbox_query(self):
        url = f'/api/custody-goods/{self.goods.id}/transition/'
        resp = self.client.post(url, {
            'status': 'released',
            'remark': '核查无误',
            'recipients': ['api-admin'],
            'idempotency_key': 'api-evt-1',
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()['data']
        self.assertFalse(data['duplicate'])
        outbox_id = data['outbox_id']

        # 重复提交同一幂等键
        duplicate = self.client.post(url, {
            'status': 'rejected',
            'idempotency_key': 'api-evt-1',
        }, format='json')
        self.assertTrue(duplicate.json()['data']['duplicate'])
        self.assertEqual(CustodyStatusLog.objects.count(), 1)

        # 投递箱列表可按状态查询
        listing = self.client.get('/api/notifications/outbox/?status=pending')
        self.assertEqual(listing.json()['data']['total'], 1)

        # 跑一轮作业后变为成功
        run_worker(max_loops=1, interval_seconds=0)
        detail = self.client.get(f'/api/notifications/outbox/{outbox_id}/')
        self.assertEqual(detail.status_code, 200)
        detail_data = detail.json()['data']
        self.assertEqual(detail_data['status'], 'succeeded')
        self.assertEqual(detail_data['deliveries'][0]['result'], 'success')

        succeeded = self.client.get('/api/notifications/outbox/?status=succeeded')
        self.assertEqual(succeeded.json()['data']['total'], 1)

    def test_illegal_transition_rejected(self):
        # 非法状态值
        resp = self.client.post(
            f'/api/custody-goods/{self.goods.id}/transition/',
            {'status': 'destroyed'}, format='json',
        )
        self.assertEqual(resp.status_code, 400)

    def test_register_and_list_custody_goods(self):
        resp = self.client.post('/api/custody-goods/', {
            'name': '证物袋', 'code': 'EV-3003',
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data']['status'], 'frozen')

        listing = self.client.get('/api/custody-goods/')
        self.assertEqual(listing.json()['data']['total'], 2)


class FlakySender(MockSender):
    """总是临时失败的发送器，用于验证未知异常路径之外的显式配置接入。"""

    def send(self, payload, recipients):
        raise TransientSendError('always down')


class ReportFailureSender(MockSender):
    """报告 ok=False 但不区分失效收件人的发送器：应按重试处理。"""

    def send(self, payload, recipients):
        from .senders import SendResult
        return SendResult(ok=False, error='gateway 500')


@override_settings(
    NOTIFICATION_SENDER='apps.warehouse.notifications.tests.FlakySender',
    NOTIFICATION_BACKOFF_BASE_SECONDS=0,
)
class ConfiguredSenderTest(TestCase):
    def test_custom_sender_path_is_used(self):
        user = User.objects.create_user('cfg-admin', 'testpass123', role='admin')
        goods = CustodyGoods.objects.create(name='x', code='EV-4004', registered_by=user)
        change_custody_status(
            goods, CustodyGoods.STATUS_RELEASED, user,
            recipients=['r1'],
        )
        outbox = NotificationOutbox.objects.get()
        outbox.max_attempts = 2
        outbox.save()
        stats = run_worker(max_loops=2, interval_seconds=0)
        self.assertEqual(stats['dead'], 1)
        self.assertEqual(NotificationOutbox.objects.get().status,
                         NotificationOutbox.STATUS_FAILED)


@override_settings(
    NOTIFICATION_SENDER='apps.warehouse.notifications.tests.ReportFailureSender',
    NOTIFICATION_BACKOFF_BASE_SECONDS=0,
    NOTIFICATION_BACKOFF_CAP_SECONDS=0,
)
class SenderReportedFailureTest(TestCase):
    def test_ok_false_without_invalid_recipients_retries(self):
        user = User.objects.create_user('rf-admin', 'testpass123', role='admin')
        goods = CustodyGoods.objects.create(name='y', code='EV-5005', registered_by=user)
        change_custody_status(
            goods, CustodyGoods.STATUS_RELEASED, user, recipients=['r1'],
        )
        stats = run_worker(max_loops=1, interval_seconds=0)
        self.assertEqual(stats['retry'], 1)
        outbox = NotificationOutbox.objects.get()
        self.assertEqual(outbox.status, NotificationOutbox.STATUS_PENDING)
        self.assertIn('gateway 500', outbox.last_error)
