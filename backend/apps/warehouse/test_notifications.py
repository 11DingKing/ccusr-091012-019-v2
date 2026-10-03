"""通知投递箱验收测试：

- 业务变更与投递箱同事务提交（不丢）；
- 重复事件幂等（不重）；
- 模拟发送器 + 可查询投递状态；
- 退避重试、重试耗尽终败、收件人失效终败；
- 作业崩溃后租约恢复；
- 并发作业不重复认领。
"""
import threading
import time
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from .custody import change_custody_status
from .models import (
    Category, CustodyStatus, Goods, NotificationDelivery, NotificationOutbox,
    Unit, Variety,
)
from .notifications.dispatcher import (
    backoff_seconds, claim_batch, enqueue_notification, process_batch,
)
from .notifications.runner import NotificationWorker
from .notifications.senders import (
    InvalidRecipientError, SimulatedSender, TransientSendError, simulated_sender,
)


def force_due():
    """把等待退避 / 租约的记录直接推到“已到期”，模拟时间流逝。"""
    past = timezone.now() - timedelta(seconds=1)
    NotificationOutbox.objects.filter(
        status__in=[NotificationOutbox.FAILED, NotificationOutbox.SENDING]
    ).update(available_at=past, lease_expires_at=past)


class NotificationFixture(TransactionTestCase):
    def setUp(self):
        simulated_sender.reset()
        self.user = User.objects.create_user("notify-user", "testpass123", role="admin")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.user)}")
        unit = Unit.objects.create(name="件", created_by=self.user)
        category = Category.objects.create(name="受控器材", unit=unit, created_by=self.user)
        variety = Variety.objects.create(name="记录终端", category=category, created_by=self.user)
        self.goods = Goods.objects.create(
            variety=variety, name="执法记录终端", code="DEV-100",
            quantity=Decimal("3"), warning_threshold=Decimal("5"),
        )

    def change(self, action='frozen', **kwargs):
        return change_custody_status(
            self.goods, action, operator=self.user,
            reason='测试', recipient='13800000000', **kwargs
        )


class SenderContractTest(TestCase):
    def setUp(self):
        simulated_sender.reset()

    def test_failure_types(self):
        self.assertTrue(issubclass(TransientSendError, Exception))
        self.assertTrue(issubclass(InvalidRecipientError, Exception))

    def test_simulated_sender_success_and_scripted_failures(self):
        sender = SimulatedSender()

        class Box:
            idempotency_key = 'k1'
            recipient = 'alice'
            event_type = 't'
            channel = 'sim'
            payload = {}

        sender.fail_times('k1', 2)
        with self.assertRaises(TransientSendError):
            sender.send(Box())
        with self.assertRaises(TransientSendError):
            sender.send(Box())
        self.assertIsNone(sender.send(Box()))
        self.assertEqual(sender.send_count('k1'), 1)

        # 重复调用幂等短路，不会产生第二条。
        self.assertIsNone(sender.send(Box()))
        self.assertEqual(sender.send_count('k1'), 1)

        sender.mark_recipient_invalid('bob')
        bob = Box()
        bob.idempotency_key = 'k2'
        bob.recipient = 'bob'
        with self.assertRaises(InvalidRecipientError):
            sender.send(bob)


class AtomicEnqueueTest(NotificationFixture):
    def test_business_and_outbox_commit_or_rollback_together(self):
        # 外层事务回滚时，业务记录与投递箱记录同时消失。
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                entry, outbox, created = self.change()
                self.assertTrue(created)
                self.assertEqual(CustodyStatus.objects.count(), 1)
                self.assertEqual(NotificationOutbox.objects.count(), 1)
                raise RuntimeError('模拟调用方在提交前崩溃 / 超时')
        self.assertEqual(CustodyStatus.objects.count(), 0)
        self.assertEqual(NotificationOutbox.objects.count(), 0)

    def test_successful_change_leaves_pending_outbox_in_same_commit(self):
        entry, outbox, created = self.change('released')
        self.assertTrue(created)
        self.assertEqual(outbox.status, NotificationOutbox.PENDING)
        self.assertEqual(outbox.aggregate_ref, f"goods:{self.goods.id}")
        self.assertEqual(CustodyStatus.objects.count(), 1)
        self.assertEqual(NotificationOutbox.objects.count(), 1)

    def test_raw_enqueue_duplicate_idempotency_key_is_swallowed(self):
        first, created1 = enqueue_notification('t', 'a', 'r', {}, idempotency_key='dup-1')
        second, created2 = enqueue_notification('t', 'a', 'r', {}, idempotency_key='dup-1')
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first.id, second.id)
        self.assertEqual(NotificationOutbox.objects.count(), 1)


class DuplicateEventTest(NotificationFixture):
    def test_same_event_id_delivers_once(self):
        _, first, created1 = self.change('frozen', event_id='evt-7')
        _, second, created2 = self.change('frozen', event_id='evt-7')
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first.id, second.id)
        self.assertEqual(NotificationOutbox.objects.count(), 1)
        self.assertEqual(CustodyStatus.objects.count(), 1)

    def test_distinct_actions_are_not_duplicates(self):
        self.change('frozen', event_id='e1')
        self.change('rejected', event_id='e1')
        self.change('released', event_id='e1')
        self.assertEqual(NotificationOutbox.objects.count(), 3)
        self.assertEqual(CustodyStatus.objects.count(), 3)


class DispatchSuccessTest(NotificationFixture):
    def test_dispatch_marks_success_and_records_attempt(self):
        _, outbox, _ = self.change()
        stats = process_batch(worker_id='w1')
        self.assertEqual(stats['claimed'], 1)
        self.assertEqual(stats['succeeded'], 1)

        outbox.refresh_from_db()
        self.assertEqual(outbox.status, NotificationOutbox.SUCCEEDED)
        self.assertEqual(outbox.attempts, 1)
        self.assertIsNotNone(outbox.sent_at)
        self.assertEqual(simulated_sender.send_count(outbox.idempotency_key), 1)

        delivery = NotificationDelivery.objects.get(outbox=outbox)
        self.assertEqual(delivery.result, NotificationDelivery.SUCCESS)
        self.assertEqual(delivery.attempt_no, 1)
        self.assertTrue(delivery.claimed_by.startswith('w1'))

        # 成功后不再被认领。
        self.assertEqual(process_batch()['claimed'], 0)


class BackoffRetryTest(NotificationFixture):
    def test_transient_failures_retry_with_backoff_then_succeed(self):
        _, outbox, _ = self.change()
        simulated_sender.fail_times(outbox.idempotency_key, 2)

        s1 = process_batch()
        outbox.refresh_from_db()
        self.assertEqual(s1['retried'], 1)
        self.assertEqual(outbox.status, NotificationOutbox.FAILED)
        self.assertEqual(outbox.attempts, 1)
        self.assertGreater(outbox.available_at, timezone.now())
        # 未到退避时间，不会被认领。
        self.assertEqual(process_batch()['claimed'], 0)

        force_due()
        s2 = process_batch()
        outbox.refresh_from_db()
        self.assertEqual(s2['retried'], 1)
        self.assertEqual(outbox.attempts, 2)

        force_due()
        s3 = process_batch()
        outbox.refresh_from_db()
        self.assertEqual(s3['succeeded'], 1)
        self.assertEqual(outbox.status, NotificationOutbox.SUCCEEDED)
        self.assertEqual(outbox.attempts, 3)
        self.assertEqual(simulated_sender.send_count(outbox.idempotency_key), 1)

        results = list(
            NotificationDelivery.objects.filter(outbox=outbox)
            .order_by('attempt_no').values_list('attempt_no', 'result')
        )
        self.assertEqual(results, [(1, 'retry'), (2, 'retry'), (3, 'success')])

    def test_backoff_is_exponential_and_capped(self):
        self.assertEqual(backoff_seconds(1), 1)
        self.assertEqual(backoff_seconds(2), 2)
        self.assertEqual(backoff_seconds(3), 4)
        self.assertLessEqual(backoff_seconds(20), 300)

    def test_exhausting_attempts_becomes_dead_and_never_retries(self):
        _, outbox, _ = self.change()
        # 永远临时失败，但 max_attempts=3，必须在三次后终败。
        outbox.max_attempts = 3
        outbox.save()
        simulated_sender.fail_times(outbox.idempotency_key, 100)

        for _ in range(3):
            process_batch()
            force_due()

        outbox.refresh_from_db()
        self.assertEqual(outbox.status, NotificationOutbox.DEAD)
        self.assertEqual(outbox.attempts, 3)
        self.assertIsNotNone(outbox.dead_at)
        self.assertIn('模拟临时故障', outbox.last_error)

        results = list(
            NotificationDelivery.objects.filter(outbox=outbox)
            .order_by('attempt_no').values_list('result', flat=True)
        )
        self.assertEqual(results, ['retry', 'retry', 'dead'])

        # 终败记录保留可查，但永远不再被认领 / 发送。
        self.assertEqual(process_batch()['claimed'], 0)
        self.assertEqual(simulated_sender.send_count(outbox.idempotency_key), 0)
        self.assertTrue(NotificationOutbox.objects.filter(pk=outbox.pk).exists())


class InvalidRecipientTest(NotificationFixture):
    def test_invalid_recipient_dead_letters_immediately_without_retry_loop(self):
        _, outbox, _ = self.change()
        simulated_sender.mark_recipient_invalid(outbox.recipient)

        stats = process_batch()
        outbox.refresh_from_db()
        self.assertEqual(stats['dead'], 1)
        self.assertEqual(outbox.status, NotificationOutbox.DEAD)
        self.assertEqual(outbox.attempts, 1)
        self.assertIsNotNone(outbox.dead_at)
        self.assertIn('收件人已失效', outbox.last_error)

        delivery = NotificationDelivery.objects.get(outbox=outbox)
        self.assertEqual(delivery.result, NotificationDelivery.DEAD)

        # 不再重试、不丢失。
        self.assertEqual(process_batch()['claimed'], 0)
        self.assertEqual(simulated_sender.send_count(outbox.idempotency_key), 0)
        self.assertTrue(NotificationOutbox.objects.filter(pk=outbox.pk).exists())


class LeaseRecoveryTest(NotificationFixture):
    def test_claimed_but_crashed_record_is_reclaimed_after_lease_expiry(self):
        _, outbox, _ = self.change()
        claimed = claim_batch(worker_id='crashed-worker', lease_seconds=30)
        self.assertEqual(len(claimed), 1)
        outbox.refresh_from_db()
        self.assertEqual(outbox.status, NotificationOutbox.SENDING)
        self.assertTrue(outbox.claimed_by.startswith('crashed-worker'))

        # 租约未到期：其他作业不能抢。
        self.assertEqual(claim_batch(worker_id='other'), [])

        # 作业崩溃，没有任何成功 / 失败记录。
        self.assertEqual(NotificationDelivery.objects.count(), 0)

        # 租约到期，新作业捞回并投递成功。
        force_due()
        stats = process_batch(worker_id='recovery-worker')
        self.assertEqual(stats['succeeded'], 1)
        outbox.refresh_from_db()
        self.assertEqual(outbox.status, NotificationOutbox.SUCCEEDED)
        self.assertTrue(outbox.claimed_by.startswith('recovery-worker'))
        self.assertEqual(simulated_sender.send_count(outbox.idempotency_key), 1)

    def test_downstream_accept_then_crash_redelivers_without_duplicate(self):
        # 下游已收到，但作业在落库成功状态前崩溃：恢复后允许重投，
        # 发送器靠幂等键短路，最终只产生一条通知。
        _, outbox, _ = self.change()
        claimed = claim_batch(worker_id='crashed-worker', lease_seconds=30)
        simulated_sender.send(claimed[0])  # 模拟“下游已接收、提交丢失”。
        self.assertEqual(simulated_sender.send_count(outbox.idempotency_key), 1)

        force_due()
        process_batch(worker_id='recovery-worker')
        outbox.refresh_from_db()
        self.assertEqual(outbox.status, NotificationOutbox.SUCCEEDED)
        self.assertEqual(simulated_sender.send_count(outbox.idempotency_key), 1)
        self.assertEqual(
            NotificationDelivery.objects.filter(
                outbox=outbox, result=NotificationDelivery.SUCCESS
            ).count(),
            1,
        )


class ConcurrentClaimTest(NotificationFixture):
    def test_concurrent_workers_do_not_double_deliver(self):
        for i in range(40):
            enqueue_notification(
                'custody.frozen', f'goods:{i}', 'r', {'n': i},
                idempotency_key=f'k-{i}',
            )

        errors = []

        def worker():
            try:
                for _ in range(20):
                    stats = process_batch(batch_size=10, lease_seconds=30)
                    if stats['claimed'] == 0:
                        break
            except Exception as exc:  # pragma: no cover - 并发不应抛错
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(
            NotificationOutbox.objects.filter(status=NotificationOutbox.SUCCEEDED).count(),
            40,
        )
        self.assertEqual(NotificationOutbox.objects.exclude(status=NotificationOutbox.SUCCEEDED).count(), 0)
        for key in [f'k-{i}' for i in range(40)]:
            self.assertEqual(simulated_sender.send_count(key), 1, key)
        # 每条只产生一次成功尝试记录。
        self.assertEqual(
            NotificationDelivery.objects.filter(result=NotificationDelivery.SUCCESS).count(),
            40,
        )


class InProcessWorkerTest(NotificationFixture):
    def test_background_worker_picks_up_records(self):
        _, outbox, _ = self.change()
        worker = NotificationWorker(interval_seconds=0.02, batch_size=10)
        worker.start()
        try:
            deadline = time.time() + 5
            while time.time() < deadline:
                outbox.refresh_from_db()
                if outbox.status == NotificationOutbox.SUCCEEDED:
                    break
                time.sleep(0.02)
            self.assertEqual(outbox.status, NotificationOutbox.SUCCEEDED)
        finally:
            worker.stop(timeout=2)
        self.assertFalse(worker.is_alive())


class CustodyApiTest(NotificationFixture):
    def test_action_endpoint_enqueues_and_status_is_queryable(self):
        response = self.client.post(
            f"/api/goods/{self.goods.id}/custody-status/",
            {'action': 'frozen', 'reason': '案件需要', 'recipient': '13800000000'},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']
        self.assertFalse(data['duplicated'])
        outbox_id = data['outbox']['id']
        self.assertEqual(data['outbox']['status'], 'pending')

        # 列表可按状态查询。
        listing = self.client.get('/api/notifications/?status=pending')
        self.assertEqual(listing.json()['data']['total'], 1)

        # 投递后详情含成功尝试记录。
        process_batch()
        detail = self.client.get(f'/api/notifications/{outbox_id}/')
        self.assertEqual(detail.status_code, 200)
        detail_data = detail.json()['data']
        self.assertEqual(detail_data['outbox']['status'], 'succeeded')
        self.assertEqual(detail_data['deliveries'][0]['result'], 'success')

    def test_duplicate_api_event_is_flagged(self):
        payload = {'action': 'released', 'event_id': 'api-evt-1'}
        first = self.client.post(
            f"/api/goods/{self.goods.id}/custody-status/", payload, format='json'
        )
        second = self.client.post(
            f"/api/goods/{self.goods.id}/custody-status/", payload, format='json'
        )
        self.assertTrue(first.json()['data']['duplicated'] is False)
        self.assertTrue(second.json()['data']['duplicated'])
        self.assertEqual(NotificationOutbox.objects.count(), 1)
        self.assertEqual(CustodyStatus.objects.count(), 1)

    def test_invalid_action_rejected(self):
        response = self.client.post(
            f"/api/goods/{self.goods.id}/custody-status/",
            {'action': 'unknown'}, format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_missing_goods_returns_404(self):
        response = self.client.post(
            '/api/goods/99999/custody-status/', {'action': 'frozen'}, format='json'
        )
        self.assertEqual(response.status_code, 404)

    def test_requires_authentication(self):
        response = APIClient().get('/api/notifications/')
        self.assertEqual(response.status_code, 401)
