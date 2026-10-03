"""运行通知投递作业：默认为常驻循环，--once 只处理一批。"""
from django.core.management.base import BaseCommand

from apps.warehouse.notifications.worker import run_worker


class Command(BaseCommand):
    help = '认领通知投递箱并分批投递（重试退避、租约恢复由作业自动处理）'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='只处理一批后退出')
        parser.add_argument('--loops', type=int, default=None, help='处理指定轮次后退出')
        parser.add_argument('--batch-size', type=int, default=None)
        parser.add_argument('--lease-seconds', type=int, default=None)
        parser.add_argument('--interval', type=int, default=None, help='常驻模式轮询间隔秒数')

    def handle(self, *args, **options):
        max_loops = 1 if options['once'] else options['loops']
        totals = run_worker(
            max_loops=max_loops,
            interval_seconds=options['interval'],
            batch_size=options['batch_size'],
            lease_seconds=options['lease_seconds'],
        )
        if max_loops is not None:
            self.stdout.write(
                f"本轮处理: 认领 {totals['claimed']}，成功 {totals['succeeded']}，"
                f"重试 {totals['retry']}，死信 {totals['dead']}"
            )
