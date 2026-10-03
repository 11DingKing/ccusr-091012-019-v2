"""
库房管理模型
"""
from django.db import models
from django.utils import timezone
from apps.authentication.models import User


class Unit(models.Model):
    """单位模型"""
    name = models.CharField('单位名称', max_length=5, unique=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_units', verbose_name='创建人'
    )
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_unit'
        verbose_name = '单位'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    @property
    def is_linked(self):
        """是否已关联至品类"""
        return self.categories.exists()


class Category(models.Model):
    """品类模型"""
    name = models.CharField('品类名称', max_length=10, unique=True)
    unit = models.ForeignKey(
        Unit, on_delete=models.PROTECT,
        related_name='categories', verbose_name='单位'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_categories', verbose_name='创建人'
    )
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_category'
        verbose_name = '品类'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    @property
    def is_linked(self):
        """是否已关联至品种"""
        return self.varieties.exists()


class Variety(models.Model):
    """品种模型"""
    name = models.CharField('品种名称', max_length=20)
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT,
        related_name='varieties', verbose_name='所属品类'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_varieties', verbose_name='创建人'
    )
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_variety'
        verbose_name = '品种'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
        unique_together = ['category', 'name']
    
    def __str__(self):
        return f"{self.category.name} - {self.name}"
    
    @property
    def is_in_stock(self):
        """是否已入库"""
        return self.goods.exists()
    
    @property
    def unit_name(self):
        """获取单位名称"""
        return self.category.unit.name if self.category and self.category.unit else ''


class Goods(models.Model):
    """货物模型"""
    variety = models.ForeignKey(
        Variety, on_delete=models.CASCADE,
        related_name='goods', verbose_name='所属品种'
    )
    name = models.CharField('货物名称', max_length=200)
    code = models.CharField('货物编码', max_length=50, unique=True)
    specification = models.CharField('规格型号', max_length=200, blank=True)
    quantity = models.DecimalField('库存数量', max_digits=12, decimal_places=2, default=0)
    warning_threshold = models.DecimalField('预警阈值', max_digits=12, decimal_places=2, default=10)
    location = models.CharField('存放位置', max_length=100, blank=True)
    remark = models.TextField('备注', blank=True)
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_goods'
        verbose_name = '货物'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    @property
    def is_warning(self):
        """是否预警"""
        return self.quantity <= self.warning_threshold


class StockIn(models.Model):
    """入库记录模型"""
    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='stock_ins', verbose_name='货物'
    )
    operator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='stock_in_operations', verbose_name='操作人'
    )
    quantity = models.DecimalField('入库数量', max_digits=12, decimal_places=2)
    batch_no = models.CharField('批次号', max_length=50, blank=True)
    supplier = models.CharField('供应商', max_length=200, blank=True)
    stock_in_time = models.DateTimeField('入库时间', auto_now_add=True)
    remark = models.TextField('备注', blank=True)
    
    class Meta:
        db_table = 'wh_stock_in'
        verbose_name = '入库记录'
        verbose_name_plural = verbose_name
        ordering = ['-stock_in_time']
    
    def __str__(self):
        return f"{self.goods.name} - {self.quantity}"


class StockOut(models.Model):
    """出库记录模型"""
    STATUS_CHOICES = [
        ('pending', '待审批'),
        ('approved', '已通过'),
        ('rejected', '已拒绝'),
        ('completed', '已完成'),
    ]
    
    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='stock_outs', verbose_name='货物'
    )
    operator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='stock_out_operations', verbose_name='操作人'
    )
    receiver = models.CharField('领用人', max_length=100)
    receiver_dept = models.CharField('领用部门', max_length=100, blank=True)
    quantity = models.DecimalField('出库数量', max_digits=12, decimal_places=2)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    stock_out_time = models.DateTimeField('出库时间', null=True, blank=True)
    remark = models.TextField('备注', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    
    class Meta:
        db_table = 'wh_stock_out'
        verbose_name = '出库记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.goods.name} - {self.quantity}"


class Warning(models.Model):
    """预警记录模型"""
    TYPE_CHOICES = [
        ('low_stock', '库存不足'),
        ('expiring', '即将过期'),
        ('expired', '已过期'),
    ]
    
    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='warnings', verbose_name='货物'
    )
    type = models.CharField('预警类型', max_length=20, choices=TYPE_CHOICES)
    message = models.TextField('预警信息')
    is_read = models.BooleanField('是否已读', default=False)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    
    class Meta:
        db_table = 'wh_warning'
        verbose_name = '预警记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.goods.name} - {self.get_type_display()}"


class Approval(models.Model):
    """审批记录模型"""
    STATUS_CHOICES = [
        ('pending', '待审批'),
        ('approved', '已通过'),
        ('rejected', '已拒绝'),
    ]
    
    stock_out = models.ForeignKey(
        StockOut, on_delete=models.CASCADE,
        related_name='approvals', verbose_name='出库记录'
    )
    approver = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='approvals', verbose_name='审批人'
    )
    status = models.CharField('审批状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    remark = models.TextField('审批意见', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_approval'
        verbose_name = '审批记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.stock_out} - {self.get_status_display()}"


class CustodyStatus(models.Model):
    """监管物资保管状态。

    记录物资在保管过程中的监管状态流转：在管 / 冻结 / 驳回 / 放行。
    每次状态变化在同一事务内写入通知投递箱，保证业务与通知的原子性。
    """
    FROZEN = 'frozen'
    REJECTED = 'rejected'
    RELEASED = 'released'
    ACTION_CHOICES = [
        (FROZEN, '冻结'),
        (REJECTED, '驳回'),
        (RELEASED, '放行'),
    ]

    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='custody_statuses', verbose_name='货物'
    )
    action = models.CharField('状态动作', max_length=20, choices=ACTION_CHOICES)
    reason = models.TextField('事由', blank=True)
    operator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='custody_operations', verbose_name='操作人'
    )
    created_at = models.DateTimeField('变更时间', auto_now_add=True)

    class Meta:
        db_table = 'wh_custody_status'
        verbose_name = '监管状态变更'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.goods.name} - {self.get_action_display()}"


class NotificationOutbox(models.Model):
    """事务性发件箱：与业务状态在同一事务内写入。

    进程内投递作业分批认领（claim）本批记录，调用模拟发送器，
    记录成功、重试退避与最终失败，崩溃后依靠租约超时恢复，不丢不重。
    """
    PENDING = 'pending'
    SENDING = 'sending'
    SUCCEEDED = 'succeeded'
    FAILED = 'failed'
    DEAD = 'dead'
    STATUS_CHOICES = [
        (PENDING, '待投递'),
        (SENDING, '投递中'),
        (SUCCEEDED, '已成功'),
        (FAILED, '待重试'),
        (DEAD, '最终失败'),
    ]

    event_type = models.CharField('事件类型', max_length=50)
    aggregate_ref = models.CharField('业务标识', max_length=100)
    idempotency_key = models.CharField('幂等键', max_length=200, unique=True)
    recipient = models.CharField('收件人', max_length=200)
    channel = models.CharField('通知渠道', max_length=20, default='simulated')
    payload = models.JSONField('通知内容', default=dict)

    status = models.CharField('投递状态', max_length=20, choices=STATUS_CHOICES, default=PENDING)
    attempts = models.PositiveIntegerField('已尝试次数', default=0)
    max_attempts = models.PositiveIntegerField('最大尝试次数', default=5)
    available_at = models.DateTimeField('下次可投递时间', default=timezone.now)
    claimed_by = models.CharField('认领者', max_length=100, blank=True, default='')
    claimed_at = models.DateTimeField('认领时间', null=True, blank=True)
    lease_expires_at = models.DateTimeField('租约到期时间', null=True, blank=True)
    last_error = models.TextField('最近错误', blank=True, default='')
    sent_at = models.DateTimeField('成功时间', null=True, blank=True)
    dead_at = models.DateTimeField('终败时间', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'wh_notification_outbox'
        verbose_name = '通知投递箱'
        verbose_name_plural = verbose_name
        ordering = ['id']
        indexes = [
            models.Index(fields=['status', 'available_at'], name='idx_outbox_dispatch'),
            models.Index(fields=['lease_expires_at'], name='idx_outbox_lease'),
        ]

    def __str__(self):
        return f"{self.event_type}:{self.aggregate_ref} -> {self.status}"


class NotificationDelivery(models.Model):
    """单次投递尝试记录，成功、每次重试与最终失败均可查询、可审计。"""
    SUCCESS = 'success'
    RETRY = 'retry'
    DEAD = 'dead'
    RESULT_CHOICES = [
        (SUCCESS, '成功'),
        (RETRY, '重试'),
        (DEAD, '最终失败'),
    ]

    outbox = models.ForeignKey(
        NotificationOutbox, on_delete=models.CASCADE,
        related_name='deliveries', verbose_name='投递箱记录'
    )
    attempt_no = models.PositiveIntegerField('第几次尝试')
    result = models.CharField('结果', max_length=20, choices=RESULT_CHOICES)
    claimed_by = models.CharField('执行者', max_length=100, blank=True, default='')
    detail = models.TextField('详情', blank=True, default='')
    created_at = models.DateTimeField('记录时间', auto_now_add=True)

    class Meta:
        db_table = 'wh_notification_delivery'
        verbose_name = '通知投递记录'
        verbose_name_plural = verbose_name
        ordering = ['id']

    def __str__(self):
        return f"outbox#{self.outbox_id} attempt {self.attempt_no} {self.result}"
