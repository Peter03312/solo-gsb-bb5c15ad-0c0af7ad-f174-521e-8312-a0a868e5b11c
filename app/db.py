"""数据库引擎、会话与建库初始化。"""

from __future__ import annotations

import hashlib
import json
import logging
import time

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

logger = logging.getLogger("downtime.db")


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(retries: int = 30, delay: float = 1.0) -> None:
    """建库建表：创建全部表结构，以及"同设备班次不重叠"的排他约束。

    带重试以等待 Compose 中数据库就绪。
    """
    from . import models  # noqa: F401  确保模型已注册到 metadata

    last_err: Exception | None = None
    for _ in range(retries):
        try:
            with engine.begin() as conn:
                Base.metadata.create_all(conn)
            break
        except OperationalError as exc:  # 数据库尚未就绪
            last_err = exc
            time.sleep(delay)
    else:
        raise RuntimeError(f"cannot initialize database: {last_err}")
    _create_exclusion_constraint()
    _migrate_change_feed()
    _migrate_adjudications()
    _migrate_segments()
    _create_rate_entries_exclusion_constraint()
    _migrate_rate_content_hashes()
    _create_maintenance_plans_exclusion_constraint()


def _create_maintenance_plans_exclusion_constraint() -> None:
    """为计划保养单增加同设备期间不重叠的排他约束（半开 [start,end)）。

    应用层已在提交事务内校验；该约束为并发/直写库场景兜底。
    依赖 btree_gist；缺失时降级为仅应用层校验并输出告警（与班次约束一致）。
    """
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
            conn.execute(
                text(
                    """
                    DO $$
                    BEGIN
                        ALTER TABLE maintenance_plans
                        ADD CONSTRAINT maintenance_plans_no_overlap_excl
                        EXCLUDE USING gist (
                            device_id WITH =,
                            tstzrange(start_ts, end_ts, '[)') WITH &&
                        );
                    EXCEPTION WHEN duplicate_object OR duplicate_table THEN
                        NULL;
                    END
                    $$;
                    """
                )
            )
    except SQLAlchemyError as exc:
        logger.warning(
            "btree_gist 不可用，未创建保养计划期间排他约束（应用层重叠校验仍生效）：%s",
            exc,
        )


def _migrate_rate_content_hashes() -> None:
    """为既有费率版本补齐规范化内容哈希与同设备唯一约束（幂等迁移）。

    新库由 create_all 直接得到非空 content_hash 和唯一约束。既有库先补列，再按
    已固化、不可变的 rate_entries 回填；若旧版本中已有相同费率表（新幂等规则启用
    前的重复发布），保留最早版本为可重试命中对象，较晚历史版本在哈希后追加版本
    消歧后缀，从而不合并、不删除任何不可变版本；最后加 NOT NULL/唯一约束。
    """
    with engine.begin() as conn:
        seen_hashes: dict[tuple[str, str], int] = {}
        conn.execute(
            text("ALTER TABLE rate_versions ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64)")
        )
        rows = conn.execute(
            text(
                """
                SELECT v.device_id, v.version
                FROM rate_versions v
                WHERE v.content_hash IS NULL
                ORDER BY v.device_id, v.version
                """
            )
        ).all()
        for device_id, version in rows:
            entries = conn.execute(
                text(
                    """
                    SELECT start_ts, end_ts, reason, amount_per_minute
                    FROM rate_entries
                    WHERE device_id = :device_id AND version = :version
                    ORDER BY start_ts, reason, end_ts, amount_per_minute, id
                    """
                ),
                {"device_id": device_id, "version": version},
            ).all()
            canonical = [
                {
                    "start": start_ts.isoformat(),
                    "end": end_ts.isoformat(),
                    "reason": reason,
                    "amount_micro": int(amount),
                }
                for start_ts, end_ts, reason, amount in entries
            ]
            raw = json.dumps(
                {"entries": canonical},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            base_hash = hashlib.sha256(raw).hexdigest()
            seen_key = (device_id, base_hash)
            duplicate_index = seen_hashes.get(seen_key, 0)
            if duplicate_index:
                # 仅给旧库重复版本消歧；截断旧哈希以满足 String(64)。
                suffix = f"#history-v{version}-{duplicate_index}"
                content_hash = base_hash[: max(0, 64 - len(suffix))] + suffix
            else:
                content_hash = base_hash
            seen_hashes[seen_key] = duplicate_index + 1
            conn.execute(
                text(
                    "UPDATE rate_versions SET content_hash = :content_hash "
                    "WHERE device_id = :device_id AND version = :version"
                ),
                {
                    "content_hash": content_hash,
                    "device_id": device_id,
                    "version": version,
                },
            )
        conn.execute(
            text("ALTER TABLE rate_versions ALTER COLUMN content_hash SET NOT NULL")
        )
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_rate_version_content_hash "
                "ON rate_versions (device_id, content_hash)"
            )
        )
        # 兼容显式表级唯一约束命名（create_all 创建的是约束而非同名索引）。
        exists = conn.execute(
            text(
                """
                SELECT 1 FROM pg_constraint
                WHERE conname = 'uq_rate_version_content_hash'
                  AND conrelid = 'rate_versions'::regclass
                """
            )
        ).scalar_one_or_none()
        if exists is None:
            conn.execute(
                text(
                    """
                    ALTER TABLE rate_versions
                    ADD CONSTRAINT uq_rate_version_content_hash
                    UNIQUE USING INDEX uq_rate_version_content_hash
                    """
                )
            )


def _create_rate_entries_exclusion_constraint() -> None:
    """为费率行增加同 (设备, 版本, 原因) 区间不重叠的排他约束（半开 [start,end)）。

    应用层已在发布事务内逐行校验；该约束为并发/直写库场景兜底。
    依赖 btree_gist；缺失时降级为仅应用层校验并输出告警（与班次约束一致）。
    """
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
            conn.execute(
                text(
                    """
                    DO $$
                    BEGIN
                        ALTER TABLE rate_entries
                        ADD CONSTRAINT rate_entries_no_overlap_excl
                        EXCLUDE USING gist (
                            device_id WITH =,
                            version WITH =,
                            reason WITH =,
                            tstzrange(start_ts, end_ts, '[)') WITH &&
                        );
                    EXCEPTION WHEN duplicate_object OR duplicate_table THEN
                        NULL;
                    END
                    $$;
                    """
                )
            )
    except SQLAlchemyError as exc:
        logger.warning(
            "btree_gist 不可用，未创建费率行排他约束（应用层重叠校验仍生效）：%s",
            exc,
        )


def _migrate_segments() -> None:
    """为既有库补齐归因区间明细结构（幂等，可重复执行）。

    - shift_result_segments 表由 create_all 直接建出；
    - shift_results 增加 segments_available 标记：本结构引入前生成的历史修订
      无法可靠还原当时的裁定状态，一律回填 FALSE（查询明细时明确返回不可用，
      不以当前状态推算）；之后生成的新修订由写路径置 TRUE 并同事务写入明细。
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE shift_results "
                "ADD COLUMN IF NOT EXISTS segments_available BOOLEAN"
            )
        )
        conn.execute(
            text(
                "UPDATE shift_results SET segments_available = FALSE "
                "WHERE segments_available IS NULL"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE shift_results "
                "ALTER COLUMN segments_available SET NOT NULL"
            )
        )
        conn.execute(
            text(
                "ALTER TABLE shift_results "
                "ALTER COLUMN segments_available SET DEFAULT TRUE"
            )
        )


def _migrate_adjudications() -> None:
    """为现场人工裁定补齐既有库结构（幂等，可重复执行）。

    manual_adjudications 由 create_all 直接建出；既有库只需把 shift_results.trigger
    加宽以容纳 adjudication / adjudication_revoke 触发器。
    """
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE shift_results ALTER COLUMN trigger TYPE VARCHAR(32)")
        )


def _migrate_change_feed() -> None:
    """为既有库补齐变更同步结构（幂等，可重复执行）。

    - shift_results 增加全局递增序号列 seq，并按原自增 id 顺序回填既有修订
      （历史数据无法还原精确提交顺序，id 顺序是最接近的可用次序）；
    - 单例计数器 change_counter 初始化为当前最大序号，之后由写事务内
      行锁递增分配（见 services._allocate_seq）。
    新建库由 create_all 直接建出完整结构，以下语句均为空操作。
    """
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE shift_results ADD COLUMN IF NOT EXISTS seq INTEGER")
        )
        conn.execute(
            text(
                """
                DO $$
                DECLARE
                    base_seq int;
                BEGIN
                    SELECT COALESCE(MAX(seq), 0) INTO base_seq FROM shift_results;
                    UPDATE shift_results r
                    SET seq = base_seq + n.rn
                    FROM (
                        SELECT id, row_number() OVER (ORDER BY id) AS rn
                        FROM shift_results
                        WHERE seq IS NULL
                    ) n
                    WHERE r.id = n.id;
                END
                $$;
                """
            )
        )
        conn.execute(text("ALTER TABLE shift_results ALTER COLUMN seq SET NOT NULL"))
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_shift_results_seq "
                "ON shift_results (seq)"
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO change_counter (id, last_seq)
                SELECT 1, COALESCE(MAX(seq), 0) FROM shift_results
                ON CONFLICT (id) DO NOTHING
                """
            )
        )


def _create_exclusion_constraint() -> None:
    """为 shifts 增加排他约束（半开区间 [start, end)，相邻不算重叠）。

    依赖 btree_gist 扩展（官方 postgres 镜像自带）；个别精简发行版缺失时
    降级为仅应用层校验并输出告警。
    """
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
            conn.execute(
                text(
                    """
                    DO $$
                    BEGIN
                        ALTER TABLE shifts
                        ADD CONSTRAINT shifts_no_overlap_excl
                        EXCLUDE USING gist (
                            device_id WITH =,
                            tstzrange(start_ts, end_ts, '[)') WITH &&
                        );
                    EXCEPTION WHEN duplicate_object OR duplicate_table THEN
                        NULL;
                    END
                    $$;
                    """
                )
            )
    except SQLAlchemyError as exc:
        logger.warning(
            "btree_gist 不可用，未创建班次排他约束（应用层重叠校验仍生效）：%s",
            exc,
        )
