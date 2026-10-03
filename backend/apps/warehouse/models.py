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


class CustodyGoods(models.Model):
    """监管物资：登记后可被冻结、驳回或放行。"""
    STATUS_FROZEN = 'frozen'
    STATUS_REJECTED = 'rejected'
    STATUS_RELEASED = 'released'
    STATUS_CHOICES = [
        (STATUS_FROZEN, '冻结'),
        (STATUS_REJECTED, '驳回'),
        (STATUS_RELEASED, '放行'),
    ]

    name = models.CharField('物资名称', max_length=200)
    code = models.CharField('物资编码', max_length=50, unique=True)
    status = models.CharField('监管状态', max_length=20, choices=STATUS_CHOICES, default=STATUS_FROZEN)
    registered_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='registered_custody_goods', verbose_name='登记人'
    )
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'wh_custody_goods'
        verbose_name = '监管物资'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} - {self.get_status_display()}"


class CustodyStatusLog(models.Model):
    """监管物资状态变化记录，其序号同时作为通知幂等键的一部分。"""
    custody_goods = models.ForeignKey(
        CustodyGoods, on_delete=models.CASCADE,
        related_name='status_logs', verbose_name='监管物资'
    )
    from_status = models.CharField('原状态', max_length=20, choices=CustodyGoods.STATUS_CHOICES)
    to_status = models.CharField('新状态', max_length=20, choices=CustodyGoods.STATUS_CHOICES)
    operator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='custody_status_logs', verbose_name='操作人'
    )
    remark = models.TextField('备注', blank=True)
    idempotency_key = models.CharField('幂等键', max_length=64, unique=True, null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'wh_custody_status_log'
        verbose_name = '监管物资状态记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.custody_goods.name}: {self.from_status} -> {self.to_status}"


class NotificationOutbox(models.Model):
    """
    通知投递箱：与业务状态变化在同一事务写入，保证业务成功即有待通知记录。

    去重依赖 (event_type, dedup_key) 唯一约束：重复提交同一状态事件只会产生一条箱记录。
    作业通过 lease_owner/lease_expires_at 实现分批认领与崩溃后的租约恢复。
    """
    EVENT_CUSTODY_STATUS_CHANGED = 'custody_status_changed'
    EVENT_CHOICES = [
        (EVENT_CUSTODY_STATUS_CHANGED, '监管物资状态变化'),
    ]

    STATUS_PENDING = 'pending'
    STATUS_PROCESSING = 'processing'
    STATUS_SUCCEEDED = 'succeeded'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING, '待投递'),
        (STATUS_PROCESSING, '投递中'),
        (STATUS_SUCCEEDED, '投递成功'),
        (STATUS_FAILED, '最终失败'),
    ]

    event_type = models.CharField('事件类型', max_length=50, choices=EVENT_CHOICES)
    dedup_key = models.CharField('幂等键', max_length=128)
    payload = models.JSONField('事件内容', default=dict)
    recipients = models.JSONField('收件人标识', default=list)
    delivered_recipients = models.JSONField('已成功收件人', default=list)

    status = models.CharField('投递状态', max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    attempts = models.PositiveIntegerField('尝试次数', default=0)
    max_attempts = models.PositiveIntegerField('最大尝试次数', default=5)
    available_at = models.DateTimeField('下次可认领时间', default=timezone.now, db_index=True)
    last_error = models.TextField('最近失败原因', blank=True, default='')

    lease_owner = models.CharField('租约持有者', max_length=64, blank=True, default='', db_index=True)
    lease_expires_at = models.DateTimeField('租约到期时间', null=True, blank=True)

    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    succeeded_at = models.DateTimeField('成功时间', null=True, blank=True)

    class Meta:
        db_table = 'wh_notification_outbox'
        verbose_name = '通知投递箱'
        verbose_name_plural = verbose_name
        ordering = ['id']
        constraints = [
            models.UniqueConstraint(
                fields=['event_type', 'dedup_key'],
                name='uniq_outbox_event_dedup',
            ),
        ]
        indexes = [
            models.Index(fields=['status', 'available_at'], name='idx_outbox_claim'),
        ]

    def __str__(self):
        return f"{self.event_type}:{self.dedup_key} ({self.status})"

    @property
    def is_terminal(self):
        return self.status in (self.STATUS_SUCCEEDED, self.STATUS_FAILED)


class NotificationDelivery(models.Model):
    """单条箱记录每一轮投递尝试的流水，可查询成功、重试与最终失败。"""
    RESULT_SUCCESS = 'success'
    RESULT_RETRY = 'retry'
    RESULT_DEAD = 'dead'
    RESULT_CHOICES = [
        (RESULT_SUCCESS, '成功'),
        (RESULT_RETRY, '重试'),
        (RESULT_DEAD, '最终失败'),
    ]

    outbox = models.ForeignKey(
        NotificationOutbox, on_delete=models.CASCADE,
        related_name='deliveries', verbose_name='投递箱记录'
    )
    attempt = models.PositiveIntegerField('第几次尝试')
    result = models.CharField('结果', max_length=10, choices=RESULT_CHOICES)
    lease_owner = models.CharField('作业实例', max_length=64, blank=True, default='')
    success_count = models.PositiveIntegerField('成功收件人数', default=0)
    failed_recipients = models.JSONField('失败收件人', default=list)
    invalid_recipients = models.JSONField('失效收件人', default=list)
    error = models.TextField('失败原因', blank=True, default='')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'wh_notification_delivery'
        verbose_name = '通知投递流水'
        verbose_name_plural = verbose_name
        ordering = ['id']

    def __str__(self):
        return f"{self.outbox_id}#{self.attempt}:{self.result}"
