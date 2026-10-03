"""一次性触发通知投递（分批认领、退避重试、终败落库）。

用法::

    python manage.py dispatch_notifications                # 跑到空闲
    python manage.py dispatch_notifications --max-batches 3
    python manage.py dispatch_notifications --batch-size 20
"""
from django.core.management.base import BaseCommand

from apps.warehouse.notifications.dispatcher import run_dispatcher


class Command(BaseCommand):
    help = '认领并处理通知投递箱中的待投递记录'

    def add_arguments(self, parser):
        parser.add_argument('--batch-size', type=int, default=None, help='每批认领数量')
        parser.add_argument('--lease-seconds', type=int, default=None, help='认租赁约秒数')
        parser.add_argument('--max-batches', type=int, default=None, help='最多处理批次数')
        parser.add_argument('--worker-id', type=str, default=None, help='作业标识')

    def handle(self, *args, **options):
        kwargs = {}
        for key in ('batch_size', 'lease_seconds', 'worker_id'):
            if options.get(key) is not None:
                kwargs[key] = options[key]
        stats = run_dispatcher(max_batches=options.get('max_batches'), **kwargs)
        self.stdout.write(
            "批次数 {batches}，认领 {claimed}，成功 {succeeded}，"
            "重试 {retried}，终败 {dead}".format(**stats)
        )
        if stats['dead']:
            self.stdout.write(self.style.WARNING(
                f"有 {stats['dead']} 条通知进入最终失败（死信），可在投递箱中查询。"
            ))
