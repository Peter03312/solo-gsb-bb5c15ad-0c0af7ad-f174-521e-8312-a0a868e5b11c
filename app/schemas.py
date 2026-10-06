"""Pydantic 请求/响应模型。

时间戳规则：必须带时区且精确到整分钟（秒与微秒为 0），否则 422。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# 保养 / 故障 / 待判
Reason = Literal["maintenance", "failure", "pending"]


def _check_ts(v: datetime) -> datetime:
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError(
            "timestamp must include a timezone offset, e.g. 2026-10-01T08:00:00+08:00"
        )
    if v.second != 0 or v.microsecond != 0:
        raise ValueError("timestamp must be aligned to a whole minute (seconds must be 00)")
    return v


class _Interval(BaseModel):
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def _ts(cls, v: datetime) -> datetime:
        return _check_ts(v)

    @model_validator(mode="after")
    def _range(self):
        if not self.start < self.end:
            raise ValueError("invalid interval: start must be earlier than end")
        return self


class DeviceCreate(BaseModel):
    id: str = Field(min_length=1, max_length=64, examples=["CNC-01"])
    name: str = Field(default="", max_length=200)


class DeviceOut(BaseModel):
    id: str
    name: str


class ShiftCreate(_Interval):
    device_id: str = Field(min_length=1, max_length=64)


class ShiftOut(BaseModel):
    id: int
    device_id: str
    start: datetime
    end: datetime


class EventIn(_Interval):
    device_id: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    reason: Reason


class EventBatchItem(_Interval):
    """断网补传的单个事件版本：字段与校验同单条上报（设备在批次级给出）。"""

    source_id: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    reason: Reason


class EventBatchIn(BaseModel):
    """现场网关断网恢复后的一次性补传：同一设备的非空事件列表。"""

    device_id: str = Field(min_length=1, max_length=64)
    events: list[EventBatchItem] = Field(min_length=1)


class ReasonMinutes(BaseModel):
    maintenance: int
    failure: int
    pending: int


class EventVersionRef(BaseModel):
    source_id: str
    version: int
    reason: str


class RecomputedShift(BaseModel):
    """迟到更正触发重算后，某个受影响班次的新修订摘要。"""

    shift_id: int
    revision: int
    totals: ReasonMinutes
    delta: ReasonMinutes


class EventAccepted(BaseModel):
    device_id: str
    source_id: str
    version: int
    idempotent: bool  # True 表示同来源同版本同内容的重复提交，未产生新数据
    recomputed: list[RecomputedShift] = []


class EventBatchItemAck(BaseModel):
    """批量补传的逐项回执：idempotent=True 表示同版本同内容的已存在项（重试）。"""

    source_id: str
    version: int
    idempotent: bool


class EventBatchAccepted(BaseModel):
    """批量补传回执：逐项标明新建/幂等，并列出实际修订的班次及差额。"""

    device_id: str
    items: list[EventBatchItemAck]
    recomputed: list[RecomputedShift] = []


# ---------------------------------------------------------------- 事件预演与确认


class ShiftImpactPreview(BaseModel):
    """预演中某个相关已结算班次的分钟数对比（结果不变的班次也列出）。"""

    shift_id: int
    revision: int  # 班次当前最新修订号
    current: ReasonMinutes  # 现有分钟数（当前最新修订）
    proposed: ReasonMinutes  # 拟变分钟数（应用候选版本后）
    delta: ReasonMinutes  # 差额 = 拟变 − 现有
    changed: bool  # 各原因分钟数是否有变化（False 的班次确认后不会产生修订）


class EventPreviewOut(BaseModel):
    """事件预演回执：只读预演，不写入事件、修订或变更。

    confirm_token 为不透明确认凭据（绑定候选内容与预演时的状态指纹），
    供 POST /events/confirm 原样回传；idempotent=True 表示同来源同版本
    同内容已存在，确认将按现有幂等语义返回且无影响（impacts 为空）。
    """

    device_id: str
    source_id: str
    version: int
    idempotent: bool
    confirm_token: str
    impacts: list[ShiftImpactPreview] = []


class EventConfirmIn(BaseModel):
    """确认提交：凭预演返回的确认凭据原子提交候选事件版本。"""

    # 凭据大小随相关已结算班次数增长（每班约百字节），上限留足余量
    confirm_token: str = Field(min_length=1, max_length=65536)


class ResultOut(BaseModel):
    shift_id: int
    device_id: str
    shift_start: datetime
    shift_end: datetime
    revision: int
    latest_revision: int
    totals: ReasonMinutes
    delta: ReasonMinutes  # 本修订相对上一修订的差额
    event_versions: list[EventVersionRef]
    trigger: str  # settle | correction
    created_at: datetime


class SettleResponse(BaseModel):
    settled: bool  # False 表示此前已结算，本次为幂等返回最新修订
    result: ResultOut


# ---------------------------------------------------------------- 归因区间明细


class SegmentBasisEvents(BaseModel):
    """事件归因依据：该区间内决定此原因的事件来源与版本列表。"""

    type: Literal["events"] = "events"
    event_versions: list[EventVersionRef]


class SegmentBasisRuling(BaseModel):
    """裁定依据：决定该段原因的生效裁定单号。"""

    type: Literal["ruling"] = "ruling"
    ruling_no: str


#: 归因区间段的依据（按 type 判别）
SegmentBasis = Annotated[
    SegmentBasisEvents | SegmentBasisRuling, Field(discriminator="type")
]


class ResultSegmentOut(BaseModel):
    """一段归因区间明细：半开区间 [start, end)，含起止、原因及依据。"""

    start: datetime
    end: datetime
    minutes: int  # 段分钟数 = (end - start)，便于直接汇总
    reason: str  # maintenance | failure | pending
    basis: SegmentBasis


class ResultSegmentsOut(BaseModel):
    """班次某修订的归因区间明细。

    - segments 按起点排序、互不重叠；只含停机分钟（未停机分钟不返回），
      各原因段分钟之和等于该修订的对应总数；
    - 明细与修订同事务固化、不可变：事件更正或裁定撤销只影响之后的新修订；
    - 本结构引入前生成的历史修订未固化明细（当时的裁定状态无法可靠还原），
      available=False 且 segments 为 null —— 明确不可用，不以当前状态推算。
    """

    shift_id: int
    revision: int
    latest_revision: int
    available: bool  # False 表示历史修订无明细（segments 为 null）
    segments: list[ResultSegmentOut] | None


class ChangeEntry(BaseModel):
    """一次结算/更正/裁定产生的变更记录（与班次修订同一行存储，内容不可变）。"""

    seq: int  # 全局递增变更序号（提交顺序），作为同步游标
    shift_id: int
    device_id: str
    shift_start: datetime
    shift_end: datetime
    revision: int
    totals: ReasonMinutes
    delta: ReasonMinutes  # 本修订相对上一修订的差额
    event_versions: list[EventVersionRef]
    trigger: str  # settle | correction | adjudication | adjudication_revoke
    created_at: datetime


class ChangePage(BaseModel):
    """一页变更；next_cursor 供下一页使用，空页保持原游标。"""

    changes: list[ChangeEntry]
    next_cursor: int


# ---------------------------------------------------------------- 班次归因快照


class ShiftSnapshotEntry(BaseModel):
    """固定变更序号下班次的归因快照条目（截至该序号的最后修订，内容不可变）。"""

    shift_id: int
    start: datetime  # 班次区间起点（半开 [start, end)，UTC 输出）
    end: datetime
    revision: int  # 序号不大于快照序号的最后修订号
    totals: ReasonMinutes  # 该修订的保养/故障/待判分钟数


class ShiftSnapshotPage(BaseModel):
    """一页班次快照。

    - snapshot_seq 全部分页保持不变；每页回传，可直接作为 GET /changes 的起始游标；
    - next_cursor 为不透明续页游标，末页为 null（null 即翻页结束）；
    - 空快照（无任何匹配班次）首页即空页，next_cursor 为 null。
    """

    device_id: str
    start: datetime
    end: datetime
    snapshot_seq: int
    shifts: list[ShiftSnapshotEntry]
    next_cursor: str | None


# ---------------------------------------------------------------- 现场人工裁定


class AdjudicationIn(_Interval):
    """已结算班次的现场人工裁定：班次内整分钟半开区间 + 原因 + 说明。"""

    ruling_no: str = Field(
        min_length=1,
        max_length=64,
        description="唯一裁定单号（班次内唯一，建议全局唯一），用于幂等与撤销",
        examples=["R-20261001-001"],
    )
    reason: Reason
    note: str = Field(default="", max_length=500, description="说明（可选）")


class AdjudicationOut(BaseModel):
    """一条现场人工裁定的当前状态（撤销留痕）。"""

    ruling_no: str
    start: datetime
    end: datetime
    reason: str
    note: str
    status: str  # active | revoked
    created_at: datetime
    revoked_at: datetime | None = None


class RevisionRef(BaseModel):
    """操作产生的班次修订摘要；未产生修订时该对象为空（response 中为 null）。"""

    revision: int
    seq: int
    totals: ReasonMinutes
    delta: ReasonMinutes
    trigger: str


# ---------------------------------------------------------------- 现场停机对账（按设备）


class FieldIntervalIn(_Interval):
    """现场停机区间（整分钟、半开）；可与窗口部分相交，服务端裁剪到窗口后合并。"""


class ReconciliationIn(BaseModel):
    """按设备对账请求：窗口 + 现场停机区间列表 + 可选固定变更序号。"""

    start: datetime
    end: datetime
    field_intervals: list[FieldIntervalIn] = Field(
        default_factory=list, description="现场停机区间列表（整分钟半开）；裁剪到窗口后合并重叠"
    )
    seq: int | None = Field(
        default=None,
        ge=0,
        description="固定变更序号；缺省时以请求开始时已提交的最大序号为准",
    )

    @field_validator("start", "end")
    @classmethod
    def _ts(cls, v: datetime) -> datetime:
        return _check_ts(v)

    @model_validator(mode="after")
    def _range(self):
        if not self.start < self.end:
            raise ValueError("invalid window: start must be earlier than end")
        return self


class ReconciliationInterval(BaseModel):
    """对账结果中的最大连续区间（半开，UTC 输出）及分钟数。"""

    start: datetime
    end: datetime
    minutes: int


class ReconciledShiftRef(BaseModel):
    """对账所用的各已结算班次及其固定序号下选取的修订（便于重放核对）。"""

    shift_id: int
    revision: int
    seq: int  # 该修订的全局变更序号（均 ≤ 对账所用序号）


class ReconciliationOut(BaseModel):
    """设备停机对账结果。

    - field_only：已结算覆盖分钟内现场有、系统无的最大连续区间（差异）；
    - system_only：系统有、现场无的最大连续区间（差异）；
    - unreconciled：现场记录落在已结算班次之外的分钟（未对账，不算差异）；
    - seq：本次实际使用的变更序号（未指定时固定为请求开始时的最新序号），可凭此重放。
    """

    device_id: str
    start: datetime
    end: datetime
    seq: int
    settled_minutes: int  # 窗口内已结算班次覆盖的分钟总数（对账范围）
    field_only: list[ReconciliationInterval]
    field_only_minutes: int
    system_only: list[ReconciliationInterval]
    system_only_minutes: int
    unreconciled: list[ReconciliationInterval]
    unreconciled_minutes: int
    reconciled_shifts: list[ReconciledShiftRef]


class AdjudicationAck(BaseModel):
    """提交/撤销裁定的统一回执：单号、状态、修订号（无修订时明确标示）。"""

    ruling_no: str
    status: str  # active | revoked
    idempotent: bool  # True 表示同单号重试/重复撤销，未产生任何修订
    revision: int | None  # 本次操作产生的修订号；无修订为 null
    latest_revision: int  # 班次当前最新修订号
    revision_detail: RevisionRef | None = None  # 本次修订摘要；无修订为 null


# ---------------------------------------------------------------- 停机损失费率表


class RateEntryIn(_Interval):
    """费率版本的一行：原因 + 整分钟半开生效区间 + 非负每分钟金额。

    金额为非负十进制字符串（元，最多 6 位小数，如 "0"、"12.5"、"0.005"）；
    同版本同原因的区间不得重叠（端点相接允许），不同原因可重叠。
    """

    reason: Reason
    amount_per_minute: str = Field(
        min_length=1,
        max_length=32,
        description="非负每分钟金额（十进制字符串，单位元，最多 6 位小数）",
        examples=["12.5"],
    )


class RateVersionIn(BaseModel):
    """发布一个完整费率版本：非空费率行列表整批原子发布。"""

    entries: list[RateEntryIn] = Field(min_length=1)
    note: str = Field(default="", max_length=500, description="版本说明（可选）")


class RateEntryOut(BaseModel):
    """费率行（回显规范十进制金额字符串，单位元）。"""

    entry_no: int
    start: datetime
    end: datetime
    reason: str
    amount_per_minute: str


class RateVersionOut(BaseModel):
    """费率版本摘要。"""

    device_id: str
    version: int
    note: str
    entry_count: int
    created_at: datetime


class RateVersionDetailOut(RateVersionOut):
    """费率版本完整费率表（按 entry_no 升序，即发布时的时间顺序）。"""

    entries: list[RateEntryOut]


class RateVersionPublishedOut(RateVersionDetailOut):
    """发布/重试费率版本的响应：标明本次是否命中原版本。"""

    idempotent: bool


# ---------------------------------------------------------------- 停机损失核算


class LossAccountingIn(BaseModel):
    """对已结算班次核算停机损失：指定修订（缺省最新）与费率版本。"""

    rate_version: int = Field(ge=1, description="设备已发布的费率版本号")
    revision: int | None = Field(
        default=None, ge=1, description="归因修订号；缺省为该班次最新修订"
    )


class LossReasonTotal(BaseModel):
    """单个原因的核算汇总。"""

    reason: str
    priced_minutes: int
    unpriced_minutes: int
    amount: str  # 金额（元，两位小数字符串）
    amount_cents: int  # 金额（分，整数）


class LossTotal(BaseModel):
    """核算合计：逐段四舍五入后求和；待定价分钟单列、不计入金额。"""

    amount: str
    amount_cents: int
    priced_minutes: int
    unpriced_minutes: int
    by_reason: list[LossReasonTotal]


class PricedPieceOut(BaseModel):
    """命中费率的切分明细：段金额已单独按分四舍五入。"""

    piece_no: int
    seg_no: int  # 来源固化归因段号
    start: datetime
    end: datetime
    minutes: int
    reason: str
    rate_per_minute: str  # 每分钟费率（元，规范十进制）
    amount: str  # 段金额（元，两位小数）
    amount_cents: int  # 段金额（分）


class UnpricedPieceOut(BaseModel):
    """待定价切分明细：未匹配任何费率的停机分钟（有分钟、无金额）。"""

    piece_no: int
    seg_no: int | None  # 首个并入的固化归因段号（可能由相邻同原因段合并）
    start: datetime
    end: datetime
    minutes: int
    reason: str


class LossAccountingOut(BaseModel):
    """按 (班次, 修订, 费率版本) 固化的停机损失账；同组合重复核算幂等返回。"""

    id: int  # 账 id：按批次分摊（loss-cost-allocations）时以此引用已固化账
    shift_id: int
    device_id: str
    revision: int
    rate_version: int
    idempotent: bool  # True 表示命中既有固化账，本次未产生新数据
    totals: LossTotal
    priced_pieces: list[PricedPieceOut]
    unpriced_pieces: list[UnpricedPieceOut]
    created_at: datetime


# ------------------------------------------------ 停机损失按批次分摊


class BatchWindowIn(_Interval):
    """一个生产批次的整分钟半开生产窗口（一批次一窗口）。"""

    batch_no: str = Field(
        min_length=1,
        max_length=64,
        description="生产批次号（单内唯一，用于按窗口归集分摊结果）",
        examples=["B-20261001-01"],
    )


class LossCostAllocationIn(BaseModel):
    """按生产批次追溯停机成本：同一设备的分摊单号、损失账 id 列表与批次窗口。

    - 全部损失账必须属于同一设备；每个班次至多引用一份账；
    - 批次窗口一批次一窗口、互不重叠（半开，端点相接允许）；
    - 任一项非法整单拒绝（不留任何部分结果）。
    """

    device_id: str = Field(min_length=1, max_length=64)
    allocation_no: str = Field(
        min_length=1,
        max_length=64,
        description="分摊单号（全局唯一；同单号同内容重试幂等，异内容冲突）",
        examples=["A-20261001-001"],
    )
    accounting_ids: list[int] = Field(
        min_length=1, description="已固化损失账 id 列表（顺序不影响同内容判定）"
    )
    batches: list[BatchWindowIn] = Field(
        min_length=1, description="生产批次窗口列表（顺序不影响同内容判定）"
    )

    @field_validator("accounting_ids")
    @classmethod
    def _accounting_ids_unique(cls, v: list[int]) -> list[int]:
        if len(set(v)) != len(v):
            raise ValueError("accounting_ids must not contain duplicates")
        return v


class AllocationReasonTotal(BaseModel):
    """某账某去向下单个原因的分钟与金额汇总。"""

    reason: str
    priced_minutes: int
    unpriced_minutes: int
    amount: str  # 定价分钟分摊金额（元，两位小数；待定价无金额但金额字段仍为 0.00）
    amount_cents: int


class AllocationDestination(BaseModel):
    """某账在一个去向上的按原因汇总（批次或未分配）。"""

    dest: str  # batch | unallocated
    batch_no: str | None
    priced_minutes: int
    unpriced_minutes: int
    amount: str
    amount_cents: int
    by_reason: list[AllocationReasonTotal]


class AllocationAccountResult(BaseModel):
    """分摊单中单份损失账的分摊结果：账头摘要、各去向按原因汇总与来源明细。"""

    accounting_id: int
    shift_id: int
    revision: int
    rate_version: int
    totals: LossTotal  # 与原损失账合计一致（分摊守恒）
    destinations: list[AllocationDestination]
    splits: list["AllocationSplitOut"]


class AllocationSplitOut(BaseModel):
    """分摊来源明细行：一条损失账切分明细与某批次窗口的交集，或窗口外的未分配段。"""

    split_no: int
    accounting_id: int
    source_piece_no: int  # 来源损失账切分明细号
    seg_no: int | None
    dest: str  # batch | unallocated
    batch_no: str | None
    start: datetime
    end: datetime
    minutes: int
    reason: str
    priced: bool
    amount: str | None  # 定价行：分摊到该去向的整数分（元两位小数）；待定价为 null
    amount_cents: int | None


class LossCostAllocationOut(BaseModel):
    """一次固化的按批次停机损失分摊（同单号同内容重试原样返回，历史不可改）。"""

    id: int
    allocation_no: str
    device_id: str
    idempotent: bool  # True 表示同单号同内容重试，返回原固化结果
    batch_windows: list[BatchWindowIn]
    accounts: list[AllocationAccountResult]
    created_at: datetime


AllocationAccountResult.model_rebuild()


# ------------------------------------------------ 按设备期间停机损失结账


class LossPeriodClosingIn(BaseModel):
    """按设备封存一个期间的停机损失结账。

    - 只纳入期间内起始（班次 start ∈ [start, end)）且截至 seq 已有结算修订的
      班次，按整班次取该序号前的最后修订，以该设备已发布的指定费率版本沿用
      现有逐段计价/待定价规则形成或复用损失账；
    - 任一选中修订缺少固化明细整单失败（不留部分新账或结账记录）；
    - 无符合班次时固化空结账；同单号同输入重试返回原结账，异输入冲突。
    """

    device_id: str = Field(min_length=1, max_length=64)
    closing_no: str = Field(
        min_length=1,
        max_length=64,
        description="结账单号（全局唯一；同单号同输入重试幂等，异输入冲突）",
        examples=["LC-202610-001"],
    )
    start: datetime = Field(description="封存期间起点（含），带时区整分钟")
    end: datetime = Field(description="封存期间终点（不含），带时区整分钟")
    seq: int = Field(
        ge=0,
        description="截止变更序号：只取 seq 不大于该序号的修订；0 表示尚无任何结算",
    )
    rate_version: int = Field(ge=1, description="该设备已发布的费率版本号")

    @field_validator("start", "end")
    @classmethod
    def _ts(cls, v: datetime) -> datetime:
        return _check_ts(v)

    @model_validator(mode="after")
    def _range(self):
        if not self.start < self.end:
            raise ValueError("invalid period: start must be earlier than end")
        return self


class ClosingReasonTotal(BaseModel):
    """结账内单个原因的汇总。"""

    reason: str
    priced_minutes: int
    unpriced_minutes: int
    amount: str  # 金额（元，两位小数字符串）
    amount_cents: int  # 金额（分，整数）


class ClosingTotals(BaseModel):
    """整单合计：等于全部纳入损失账合计之和；待定价分钟单列、不计金额。"""

    amount: str
    amount_cents: int
    priced_minutes: int
    unpriced_minutes: int
    by_reason: list[ClosingReasonTotal]


class ClosingItemOut(BaseModel):
    """结账内单班次条目：所选修订、账引用、汇总与账内切分明细。"""

    item_no: int
    shift_id: int
    shift_start: datetime  # 班次区间（UTC 输出）
    shift_end: datetime
    revision: int
    revision_seq: int  # 所选修订的全局变更序号（均 <= 结账截止序号）
    accounting_id: int  # 复用/新形成的损失账 id
    rate_version: int
    totals: LossTotal
    priced_pieces: list[PricedPieceOut]
    unpriced_pieces: list[UnpricedPieceOut]


class LossPeriodClosingOut(BaseModel):
    """一次固化的按设备期间停机损失结账（历史不可改；同单号同输入重试原样返回）。"""

    id: int
    closing_no: str
    device_id: str
    idempotent: bool  # True 表示同单号同输入重试，返回原固化结账
    start: datetime
    end: datetime
    seq: int  # 截止变更序号
    rate_version: int
    totals: ClosingTotals
    items: list[ClosingItemOut]
    created_at: datetime


# ------------------------------------------------ 计划保养核对


class MaintenanceWindowIn(_Interval):
    """一个计划保养窗口（整分钟、半开）；须落在计划期间内，窗口间互不重叠。"""


class MaintenancePlanIn(BaseModel):
    """车间按设备提交的计划保养单。

    - 设备号 + 全局唯一单号 + 带时区整分钟的半开期间 [start, end)；
    - windows 为非空保养窗口列表：每窗口完整落在期间内、窗口互不重叠
      （半开，端点相接允许）；同设备各计划单的期间不得重叠；
    - 提交后不可改：同单号同内容重试幂等（窗口换序、等价时区仍算同内容），
      同单号异内容冲突。
    """

    device_id: str = Field(min_length=1, max_length=64)
    plan_no: str = Field(
        min_length=1,
        max_length=64,
        description="全局唯一保养计划单号：同单号同内容重试幂等，异内容冲突",
        examples=["MP-202610-001"],
    )
    start: datetime = Field(description="计划期间起点（含），带时区整分钟")
    end: datetime = Field(description="计划期间终点（不含），带时区整分钟")
    windows: list[MaintenanceWindowIn] = Field(
        min_length=1, description="保养窗口列表（顺序不影响同内容判定）"
    )

    @field_validator("start", "end")
    @classmethod
    def _ts(cls, v: datetime) -> datetime:
        return _check_ts(v)

    @model_validator(mode="after")
    def _range(self):
        if not self.start < self.end:
            raise ValueError("invalid plan period: start must be earlier than end")
        return self


class MaintenanceWindowOut(BaseModel):
    """固化的保养窗口（按起点升序，UTC 输出）。"""

    start: datetime
    end: datetime
    minutes: int


class MaintenancePlanOut(BaseModel):
    """一次固化的计划保养单（提交后不可改；同单号同内容重试原样返回）。"""

    id: int
    plan_no: str
    device_id: str
    idempotent: bool  # True 表示同单号同内容重试，返回原固化单
    start: datetime
    end: datetime
    windows: list[MaintenanceWindowOut]
    created_at: datetime


class MaintenanceInterval(BaseModel):
    """核对结果中的最大连续半开区间（UTC 输出）及分钟数。"""

    start: datetime
    end: datetime
    minutes: int


class MaintenanceShiftBasis(BaseModel):
    """核对所用的各已结算班次及其固定序号下选取的修订（便于重放核对）。"""

    shift_id: int
    revision: int
    seq: int  # 该修订的全局变更序号（均 ≤ 核对所用序号）
    shift_start: datetime
    shift_end: datetime


class MaintenanceCheckOut(BaseModel):
    """按设备核对计划保养执行情况的结果（固定变更序号，可重放）。

    - executed（已执行）：保养窗口与"保养"归因段的交集最大连续区间；
    - not_executed（未执行）：保养窗口内已被已结算班次覆盖、但非保养的分钟
      （故障/待判，以及无停机归因的正常生产分钟）；
    - unplanned（计划外）：期间内保养窗口之外出现的保养段区间；
    - unverified（未核实）：保养窗口中未被任何已结算班次覆盖的分钟
      （单列，不计未执行）；
    - seq：本次实际使用的变更序号（缺省固定为查询开始时最新序号），
      basis 给出所用班次修订依据，可据此重放。
    """

    plan_no: str
    device_id: str
    start: datetime
    end: datetime
    seq: int
    executed: list[MaintenanceInterval]
    executed_minutes: int
    not_executed: list[MaintenanceInterval]
    not_executed_minutes: int
    unplanned: list[MaintenanceInterval]
    unplanned_minutes: int
    unverified: list[MaintenanceInterval]
    unverified_minutes: int
    window_minutes: int  # 全部保养窗口分钟总数（已执行 + 未执行 + 未核实）
    covered_minutes: int  # 期间内已结算班次覆盖的分钟总数
    basis: list[MaintenanceShiftBasis]
