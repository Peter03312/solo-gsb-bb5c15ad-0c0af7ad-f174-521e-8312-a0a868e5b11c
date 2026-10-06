#!/usr/bin/env bash
# 冒烟脚本：走完 设备→班次→事件→幂等/冲突→结算→迟到更正→现场裁定/撤销→断网批量补传→事件预演/确认→修订/变更查询→班次快照→现场对账→费率发布→停机损失核算→按批次分摊停机成本→按设备期间停机损失结账 全流程。# 计划保养提交与核对
# 用法：./scripts/smoke.sh [BASE_URL]   （默认 http://localhost:8000）
set -euo pipefail

BASE="${1:-http://localhost:8000}"

req() {  # req <method> <path> [json]
  local method="$1" path="$2" body="${3:-}"
  echo "==> $method $path"
  if [ -n "$body" ]; then
    curl -sS -X "$method" "$BASE$path" -H 'Content-Type: application/json' -d "$body"
  else
    curl -sS -X "$method" "$BASE$path"
  fi
  echo; echo
}

req GET /health

req POST /devices '{"id":"CNC-01","name":"加工中心1号"}'

req POST /shifts '{"device_id":"CNC-01","start":"2026-10-01T08:00:00+08:00","end":"2026-10-01T16:00:00+08:00"}'

echo "-- 以下请求预期 409（班次重叠）--"
req POST /shifts '{"device_id":"CNC-01","start":"2026-10-01T15:00:00+08:00","end":"2026-10-01T20:00:00+08:00"}'

req POST /events '{"device_id":"CNC-01","source_id":"MES-1","version":1,"start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","reason":"maintenance"}'

echo "-- 重复提交同一事件版本，预期 200 且 idempotent=true --"
req POST /events '{"device_id":"CNC-01","source_id":"MES-1","version":1,"start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","reason":"maintenance"}'

echo "-- 同版本异内容，预期 409 VERSION_CONFLICT --"
req POST /events '{"device_id":"CNC-01","source_id":"MES-1","version":1,"start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:30:00+08:00","reason":"maintenance"}'

echo "-- 跳号，预期 409 VERSION_GAP --"
req POST /events '{"device_id":"CNC-01","source_id":"MES-1","version":3,"start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","reason":"maintenance"}'

req POST /events '{"device_id":"CNC-01","source_id":"PLC-9","version":1,"start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T12:00:00+08:00","reason":"failure"}'

req POST /shifts/1/settle

echo "-- 迟到更正：故障缩短为 10:30-11:30，预期 recomputed 中 revision=2、failure 差额 -30 --"
req POST /events '{"device_id":"CNC-01","source_id":"PLC-9","version":2,"start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T11:30:00+08:00","reason":"failure"}'

req GET "/shifts/1/result"
req GET "/shifts/1/result?revision=1"
req GET "/shifts/1/revisions"

echo "-- 现场人工裁定：10:30-11:30 改判故障（11:00-11:30 本就是故障），预期 revision=3、maintenance -30 / failure +30 --"
req POST /shifts/1/adjudications '{"ruling_no":"R-001","start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T11:30:00+08:00","reason":"failure","note":"现场核对为传动故障"}'

echo "-- 同单号同内容重试：幂等 200，revision=null（无修订明确标示）--"
req POST /shifts/1/adjudications '{"ruling_no":"R-001","start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T11:30:00+08:00","reason":"failure","note":"现场核对为传动故障"}'

echo "-- 同单号异内容：预期 409 RULING_CONFLICT --"
req POST /shifts/1/adjudications '{"ruling_no":"R-001","start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T11:30:00+08:00","reason":"pending","note":"改判待判"}'

echo "-- 更晚生效的重叠裁定 R-002：11:00-12:00 待判，重叠分钟以它为准 --"
req POST /shifts/1/adjudications '{"ruling_no":"R-002","start":"2026-10-01T11:00:00+08:00","end":"2026-10-01T12:00:00+08:00","reason":"pending","note":"二次裁定"}'

echo "-- 裁定列表（含已撤销，按生效顺序）--"
req GET /shifts/1/adjudications

echo "-- 按单号撤销 R-002：重叠分钟回退至 R-001，产生 adjudication_revoke 修订 --"
req POST /shifts/1/adjudications/R-002/revoke

echo "-- 重复撤销：幂等 200，revision=null --"
req POST /shifts/1/adjudications/R-002/revoke

echo "-- 归因区间明细：最新修订（含裁定依据段），各原因段分钟之和应等于修订总数 --"
req GET "/shifts/1/result/segments"

echo "-- 指定修订的明细：修订 3（裁定 R-001 生效时）与修订 4（R-002 覆盖时），固化后不可变 --"
req GET "/shifts/1/result/segments?revision=3"
req GET "/shifts/1/result/segments?revision=4"

echo "-- 未知班次 / 未知修订的明细查询：预期 404 SHIFT_NOT_FOUND / REVISION_NOT_FOUND --"
req GET "/shifts/99999/result/segments"
req GET "/shifts/1/result/segments?revision=99"

echo "-- 断网补传（批量上报）：MES-1 v2 新建 + PLC-9 v2 幂等重试，预期 201，班次只产生一次修订 --"
req POST /events/batch '{"device_id":"CNC-01","events":[
  {"source_id":"MES-1","version":2,"start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T12:00:00+08:00","reason":"maintenance"},
  {"source_id":"PLC-9","version":2,"start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T11:30:00+08:00","reason":"failure"}
]}'

echo "-- 整批原样重发：全部幂等，预期 200 且 recomputed 为空 --"
req POST /events/batch '{"device_id":"CNC-01","events":[
  {"source_id":"MES-1","version":2,"start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T12:00:00+08:00","reason":"maintenance"},
  {"source_id":"PLC-9","version":2,"start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T11:30:00+08:00","reason":"failure"}
]}'

echo "-- 批次含同版本异内容项：预期 409 VERSION_CONFLICT，整批失败（PLC-9 v3 不落库）--"
req POST /events/batch '{"device_id":"CNC-01","events":[
  {"source_id":"PLC-9","version":3,"start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T12:00:00+08:00","reason":"failure"},
  {"source_id":"MES-1","version":2,"start":"2026-10-01T09:00:00+08:00","end":"2026-10-01T12:00:00+08:00","reason":"maintenance"}
]}'

echo "-- 批次跳号：预期 409 VERSION_GAP，整批失败 --"
req POST /events/batch '{"device_id":"CNC-01","events":[
  {"source_id":"MES-1","version":4,"start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T12:00:00+08:00","reason":"maintenance"}
]}'

echo "-- 批次失败后版本链未推进：PLC-9 v3 仍可经单条正常上报 --"
req POST /events '{"device_id":"CNC-01","source_id":"PLC-9","version":3,"start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T12:00:00+08:00","reason":"failure"}'

echo "-- 事件预演：PLC-9 v4 故障缩短为 10:30-11:00，只读预览对已结算班次的影响并返回确认凭据 --"
PREVIEW="$(curl -sS -X POST "$BASE/events/preview" -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"PLC-9","version":4,"start":"2026-10-01T10:30:00+08:00","end":"2026-10-01T11:00:00+08:00","reason":"failure"}')"
echo "$PREVIEW"; echo
TOKEN="$(echo "$PREVIEW" | python3 -c 'import sys,json;print(json.load(sys.stdin)["confirm_token"])')"

echo "-- 确认提交：凭确认凭据原子落库，预期 201 且 recomputed 与预演影响一致 --"
req POST /events/confirm "{\"confirm_token\":\"$TOKEN\"}"

echo "-- 相同确认重试：遵循事件幂等语义，预期 200 且 idempotent=true --"
req POST /events/confirm "{\"confirm_token\":\"$TOKEN\"}"

echo "-- 预演后状态已变：先预演 MES-1 v3（同内容，影响列出但 changed=false），再上报新事件使凭据过期 --"
PREVIEW2="$(curl -sS -X POST "$BASE/events/preview" -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"MES-1","version":3,"start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T12:00:00+08:00","reason":"maintenance"}')"
echo "$PREVIEW2"; echo
TOKEN2="$(echo "$PREVIEW2" | python3 -c 'import sys,json;print(json.load(sys.stdin)["confirm_token"])')"
req POST /events '{"device_id":"CNC-01","source_id":"ENV-1","version":1,"start":"2026-10-01T13:00:00+08:00","end":"2026-10-01T14:00:00+08:00","reason":"pending"}'

echo "-- 凭过期凭据确认：预期 409 PREVIEW_STALE，不写入任何数据 --"
req POST /events/confirm "{\"confirm_token\":\"$TOKEN2\"}"

echo "-- 重新预演后再确认：预期 201（同内容新版本，分钟数不变故 recomputed 为空）--"
PREVIEW3="$(curl -sS -X POST "$BASE/events/preview" -H 'Content-Type: application/json' \
  -d '{"device_id":"CNC-01","source_id":"MES-1","version":3,"start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T12:00:00+08:00","reason":"maintenance"}')"
TOKEN3="$(echo "$PREVIEW3" | python3 -c 'import sys,json;print(json.load(sys.stdin)["confirm_token"])')"
req POST /events/confirm "{\"confirm_token\":\"$TOKEN3\"}"

echo "-- 非法确认凭据：预期 422 INVALID_CONFIRM_TOKEN --"
req POST /events/confirm '{"confirm_token":"not-a-valid-token"}'

echo "-- 越界区间 / 未结算校验：越界 422 RULING_OUT_OF_RANGE，未知单号撤销 404 --"
req POST /shifts/1/adjudications '{"ruling_no":"RX","start":"2026-10-01T07:00:00+08:00","end":"2026-10-01T09:00:00+08:00","reason":"failure","note":"越界"}'
req POST /shifts/1/adjudications/NOPE/revoke

echo "-- 变更同步：从游标 0 回放（每页 1 条），再用 next_cursor 翻页 --"
req GET "/changes?cursor=0&limit=1"
req GET "/changes?cursor=1&limit=10"

echo "-- 已追平：空页保持原游标（游标取当前 next_cursor）--"
LATEST_CURSOR="$(curl -sS "$BASE/changes?cursor=0&limit=500" | python3 -c 'import sys,json;print(json.load(sys.stdin)["next_cursor"])')"
echo "当前最大游标：$LATEST_CURSOR"
req GET "/changes?cursor=$LATEST_CURSOR"

echo "-- 非法游标 / 超限页大小，预期 422 --"
req GET "/changes?cursor=-1"
req GET "/changes?cursor=999999"
req GET "/changes?limit=1000"

echo "-- 班次归因快照（首次接入/重建索引）：窗口 10/1-10/4，每页 1 条，首页不指定 seq 固定为当前最大序号 --"
SNAP1="$(curl -sS "$BASE/devices/CNC-01/shift-snapshots?start=2026-10-01T00:00:00%2B08:00&end=2026-10-04T00:00:00%2B08:00&limit=1")"
echo "$SNAP1"; echo
SNAP_SEQ="$(echo "$SNAP1" | python3 -c 'import sys,json;print(json.load(sys.stdin)["snapshot_seq"])')"
SNAP_CURSOR="$(echo "$SNAP1" | python3 -c 'import sys,json,urllib.parse;c=json.load(sys.stdin)["next_cursor"];print(urllib.parse.quote(c) if c is not None else "")')"
echo "快照序号：$SNAP_SEQ（可直接作为 /changes 的起始游标）"

if [ -n "$SNAP_CURSOR" ]; then
  echo "-- 快照续页：原样回传不透明游标，末页 next_cursor 应为 null --"
  req GET "/devices/CNC-01/shift-snapshots?start=2026-10-01T00:00:00%2B08:00&end=2026-10-04T00:00:00%2B08:00&limit=1&cursor=$SNAP_CURSOR"
else
  echo "-- 本窗口仅一个班次：首页即末页，next_cursor=null（无续页）--"
  # 再造一个已结算班次以演示续页游标
  req POST /shifts '{"device_id":"CNC-01","start":"2026-10-02T08:00:00+08:00","end":"2026-10-02T16:00:00+08:00"}'
  req POST /shifts/2/settle
  SNAP1="$(curl -sS "$BASE/devices/CNC-01/shift-snapshots?start=2026-10-01T00:00:00%2B08:00&end=2026-10-04T00:00:00%2B08:00&limit=1")"
  SNAP_CURSOR="$(echo "$SNAP1" | python3 -c 'import sys,json,urllib.parse;print(urllib.parse.quote(json.load(sys.stdin)["next_cursor"]))')"
  echo "-- 快照续页：原样回传不透明游标，第二页 next_cursor 应为 null --"
  req GET "/devices/CNC-01/shift-snapshots?start=2026-10-01T00:00:00%2B08:00&end=2026-10-04T00:00:00%2B08:00&limit=1&cursor=$SNAP_CURSOR"
fi

echo "-- 固定 seq=1 重建索引：只含当时已结算班次的截至修订（历史不可变）--"
req GET "/devices/CNC-01/shift-snapshots?start=2026-10-01T00:00:00%2B08:00&end=2026-10-04T00:00:00%2B08:00&seq=1"

echo "-- 快照序号衔接变更流：cursor=$SNAP_SEQ（有新变更则列出，否则空页保持游标）--"
req GET "/changes?cursor=$SNAP_SEQ"

echo "-- 快照非法参数：序号超范围 422 INVALID_SNAPSHOT_SEQ / 游标不匹配 422 INVALID_CURSOR / 缺时区 422 / 未知设备 404 --"
req GET "/devices/CNC-01/shift-snapshots?start=2026-10-01T00:00:00%2B08:00&end=2026-10-04T00:00:00%2B08:00&seq=999999"
req GET "/devices/CNC-01/shift-snapshots?start=2026-10-02T00:00:00%2B08:00&end=2026-10-04T00:00:00%2B08:00&seq=1&cursor=$SNAP_CURSOR"
req GET "/devices/CNC-01/shift-snapshots?start=2026-10-01T00:00:00&end=2026-10-04T00:00:00%2B08:00"
req GET "/devices/NOPE/shift-snapshots?start=2026-10-01T00:00:00%2B08:00&end=2026-10-04T00:00:00%2B08:00"

echo "-- 非法区间 / 未知设备，预期 422 / 404 --"
req POST /shifts '{"device_id":"CNC-01","start":"2026-10-02T16:00:00+08:00","end":"2026-10-02T08:00:00+08:00"}'
req POST /events '{"device_id":"NOPE","source_id":"X","version":1,"start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","reason":"failure"}'

echo "-- 现场停机对账：窗口 10/1，现场报 09:30-10:30 + 班次外 20:00-21:00；seq 缺省固定当前最新 --"
req POST /devices/CNC-01/downtime-reconciliation '{"start":"2026-10-01T00:00:00+08:00","end":"2026-10-02T00:00:00+08:00","field_intervals":[
  {"start":"2026-10-01T09:30:00+08:00","end":"2026-10-01T10:30:00+08:00"},
  {"start":"2026-10-01T20:00:00+08:00","end":"2026-10-01T21:00:00+08:00"}]}'

echo "-- 固定 seq=1 重放（取各班次截至序号 1 的最后修订；结果携带所用序号）--"
req POST /devices/CNC-01/downtime-reconciliation '{"start":"2026-10-01T00:00:00+08:00","end":"2026-10-02T00:00:00+08:00","seq":1,"field_intervals":[
  {"start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T12:00:00+08:00"}]}'

echo "-- 对账序号越界：预期 422 INVALID_SEQ --"
req POST /devices/CNC-01/downtime-reconciliation '{"start":"2026-10-01T00:00:00+08:00","end":"2026-10-02T00:00:00+08:00","seq":999999,"field_intervals":[]}'

echo "-- 对账非法窗口（缺时区 / start≥end）与未知设备：预期 422 / 404 --"
req POST /devices/CNC-01/downtime-reconciliation '{"start":"2026-10-01T00:00:00","end":"2026-10-02T00:00:00+08:00","field_intervals":[]}'
req POST /devices/NOPE/downtime-reconciliation '{"start":"2026-10-02T00:00:00+08:00","end":"2026-10-01T00:00:00+08:00","field_intervals":[]}'

echo "-- 费率发布 v1（完整费率表；故障全天 1 元/分、保养 2 元/分、待判 0 元/分），预期 201 version=1 --"
req POST /devices/CNC-01/rate-versions '{"note":"2026Q4","entries":[
  {"reason":"failure","start":"2026-10-01T00:00:00+08:00","end":"2026-10-02T00:00:00+08:00","amount_per_minute":"1"},
  {"reason":"maintenance","start":"2026-10-01T00:00:00+08:00","end":"2026-10-02T00:00:00+08:00","amount_per_minute":"2"},
  {"reason":"pending","start":"2026-10-01T00:00:00+08:00","end":"2026-10-02T00:00:00+08:00","amount_per_minute":"0"}]}'

echo "-- 费率发布 v2（仅故障费率，10:00 边界 1.5 元、其后 1 元；端点相接合法），预期 version=2 --"
req POST /devices/CNC-01/rate-versions '{"entries":[
  {"reason":"failure","start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","amount_per_minute":"1.5"},
  {"reason":"failure","start":"2026-10-01T11:00:00+08:00","end":"2026-10-02T00:00:00+08:00","amount_per_minute":"1"}]}'

echo "-- 费率版本列表与读取 --"
req GET /devices/CNC-01/rate-versions
req GET /devices/CNC-01/rate-versions/1

echo "-- 同设备相同费率表重试（等价 UTC、行换序、金额写法等价、note 省略）：预期 200 仍返回 version=1 --"
req POST /devices/CNC-01/rate-versions '{"entries":[
  {"reason":"pending","start":"2026-09-30T16:00:00Z","end":"2026-10-01T16:00:00Z","amount_per_minute":"0.000000"},
  {"reason":"maintenance","start":"2026-09-30T16:00:00Z","end":"2026-10-01T16:00:00Z","amount_per_minute":"2.0"},
  {"reason":"failure","start":"2026-09-30T16:00:00Z","end":"2026-10-01T16:00:00Z","amount_per_minute":"1.0"}]}'

echo "-- 非法费率表（同原因区间重叠 / 负金额 / 空表）：预期 422 RATE_ENTRY_INVALID（整批不留痕）--"
req POST /devices/CNC-01/rate-versions '{"entries":[
  {"reason":"failure","start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T12:00:00+08:00","amount_per_minute":"1"},
  {"reason":"failure","start":"2026-10-01T11:00:00+08:00","end":"2026-10-01T13:00:00+08:00","amount_per_minute":"2"}]}'
req POST /devices/CNC-01/rate-versions '{"entries":[
  {"reason":"failure","start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00","amount_per_minute":"-1"}]}'

echo "-- 损失核算：班次 1 修订 1 × 费率版本 2（仅故障费率）；保养 60 分钟应单列待定价 --"
req POST /shifts/1/loss-accountings '{"revision":1,"rate_version":2}'

echo "-- 同组合重复核算：预期 200 幂等（idempotent=true，内容与首次一致）--"
req POST /shifts/1/loss-accountings '{"revision":1,"rate_version":2}'

echo "-- 新组合：修订 1 × 费率版本 1（全费率，无待定价），预期 201 新账 --"
req POST /shifts/1/loss-accountings '{"revision":1,"rate_version":1}'

echo "-- 列出班次全部固化账（历史账不可改写，可按修订/费率版本过滤）--"
req GET /shifts/1/loss-accountings
req GET "/shifts/1/loss-accountings?revision=1&rate_version=1"

echo "-- 损失核算错误：未知费率版本 404 / 历史修订无明细 422 SEGMENTS_UNAVAILABLE / 未知班次 404 --"
req POST /shifts/1/loss-accountings '{"revision":1,"rate_version":999}'
req POST /shifts/9999/loss-accountings '{"rate_version":1}'

echo "-- 按生产批次分摊停机成本：修订 1 × 费率版本 1 的账（id=2，保养 60×2 + 故障 60×1 = 180.00 元）--"
echo "-- 批次 B1001 覆盖 10:00-11:00（保养 60 分钟 120.00）、B1002 覆盖 11:00-11:30（故障 30 分钟 30.00），11:30-12:00 归未分配（故障 30 分钟 30.00）--"
req POST /devices/CNC-01/loss-cost-allocations '{"device_id":"CNC-01","allocation_no":"AL-SMOKE-001","accounting_ids":[2],"batches":[
  {"batch_no":"B1002","start":"2026-10-01T11:00:00+08:00","end":"2026-10-01T11:30:00+08:00"},
  {"batch_no":"B1001","start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00"}]}'

echo "-- 同单号同内容重试（列表顺序不影响）：预期 200 幂等，idempotent=true --"
req POST /devices/CNC-01/loss-cost-allocations '{"device_id":"CNC-01","allocation_no":"AL-SMOKE-001","accounting_ids":[2],"batches":[
  {"batch_no":"B1001","start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00"},
  {"batch_no":"B1002","start":"2026-10-01T11:00:00+08:00","end":"2026-10-01T11:30:00+08:00"}]}'

echo "-- 按单号读取固化分摊；列出设备全部分摊单 --"
req GET /loss-cost-allocations/AL-SMOKE-001
req GET /devices/CNC-01/loss-cost-allocations

echo "-- 同单号异内容：预期 409 ALLOCATION_CONFLICT（原结果不变）--"
req POST /devices/CNC-01/loss-cost-allocations '{"device_id":"CNC-01","allocation_no":"AL-SMOKE-001","accounting_ids":[2],"batches":[
  {"batch_no":"B1001","start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T10:30:00+08:00"}]}'

echo "-- 非法分摊整单拒绝：同班次两份账 422 ACCOUNTINGS_SAME_SHIFT / 批次窗口重叠 422 BATCH_WINDOW_INVALID / 未知账 404 --"
req POST /devices/CNC-01/loss-cost-allocations '{"device_id":"CNC-01","allocation_no":"AL-SMOKE-BAD1","accounting_ids":[1,2],"batches":[
  {"batch_no":"B1","start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00"}]}'
req POST /devices/CNC-01/loss-cost-allocations '{"device_id":"CNC-01","allocation_no":"AL-SMOKE-BAD2","accounting_ids":[2],"batches":[
  {"batch_no":"B1","start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:30:00+08:00"},
  {"batch_no":"B2","start":"2026-10-01T11:00:00+08:00","end":"2026-10-01T12:00:00+08:00"}]}'
req POST /devices/CNC-01/loss-cost-allocations '{"device_id":"CNC-01","allocation_no":"AL-SMOKE-BAD3","accounting_ids":[999],"batches":[
  {"batch_no":"B1","start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00"}]}'

echo "-- 期间停机损失结账：快照章节已建 10/2 08:00-16:00 班次（id=2，已空结算 rev1）；为其上报故障 09:00-10:00（更正为 rev2）后封存 --"
echo "-- 费率版本 1 的生效区间只到 10/2 00:00（+08），班次 2 的故障 60 分钟将落入待定价（有分钟、无金额）--"
SHIFT2=2
req POST /events "{\"device_id\":\"CNC-01\",\"source_id\":\"GW-2\",\"version\":1,\"start\":\"2026-10-02T09:00:00+08:00\",\"end\":\"2026-10-02T10:00:00+08:00\",\"reason\":\"failure\"}"
req POST "/shifts/$SHIFT2/settle"

# 取当前已提交最大变更序号作为结账截止序号
CLOSING_SEQ="$(curl -sS "$BASE/changes?cursor=0&limit=500" | python3 -c 'import sys,json;print(json.load(sys.stdin)["next_cursor"])')"
echo "结账截止序号：$CLOSING_SEQ（含班次 1 各修订与班次 $SHIFT2 结算）"

req POST /devices/CNC-01/loss-closings "{
  \"device_id\":\"CNC-01\",\"closing_no\":\"LC-SMOKE-001\",
  \"start\":\"2026-10-01T00:00:00+08:00\",\"end\":\"2026-10-03T00:00:00+08:00\",
  \"seq\":$CLOSING_SEQ,\"rate_version\":1}"

echo "-- 同单号同输入重试：预期 200 幂等，idempotent=true，账引用与首次一致 --"
req POST /devices/CNC-01/loss-closings "{
  \"device_id\":\"CNC-01\",\"closing_no\":\"LC-SMOKE-001\",
  \"start\":\"2026-10-01T00:00:00+08:00\",\"end\":\"2026-10-03T00:00:00+08:00\",
  \"seq\":$CLOSING_SEQ,\"rate_version\":1}"

echo "-- 按单号读取固化结账；列出设备全部结账 --"
req GET /loss-closings/LC-SMOKE-001
req GET /devices/CNC-01/loss-closings

echo "-- 同单号异输入（改费率版本）：预期 409 CLOSING_CONFLICT，原结账不变 --"
req POST /devices/CNC-01/loss-closings "{
  \"device_id\":\"CNC-01\",\"closing_no\":\"LC-SMOKE-001\",
  \"start\":\"2026-10-01T00:00:00+08:00\",\"end\":\"2026-10-03T00:00:00+08:00\",
  \"seq\":$CLOSING_SEQ,\"rate_version\":2}"

echo "-- 空结账（11 月期间内无班次）：预期 201，items 为空、合计全 0 --"
req POST /devices/CNC-01/loss-closings "{
  \"device_id\":\"CNC-01\",\"closing_no\":\"LC-SMOKE-EMPTY\",
  \"start\":\"2026-11-01T00:00:00+08:00\",\"end\":\"2026-11-02T00:00:00+08:00\",
  \"seq\":$CLOSING_SEQ,\"rate_version\":1}"

echo "-- 结账错误：序号越界 422 INVALID_SEQ / 未知费率版本 404 / 未知单号 404 / 非法期间 422 --"
req POST /devices/CNC-01/loss-closings '{"device_id":"CNC-01","closing_no":"LC-SMOKE-BAD1","start":"2026-10-01T00:00:00+08:00","end":"2026-10-03T00:00:00+08:00","seq":999999,"rate_version":1}'
req POST /devices/CNC-01/loss-closings '{"device_id":"CNC-01","closing_no":"LC-SMOKE-BAD2","start":"2026-10-01T00:00:00+08:00","end":"2026-10-03T00:00:00+08:00","seq":1,"rate_version":999}'
req GET /loss-closings/NOPE
req POST /devices/CNC-01/loss-closings '{"device_id":"CNC-01","closing_no":"LC-SMOKE-BAD3","start":"2026-10-03T00:00:00+08:00","end":"2026-10-01T00:00:00+08:00","seq":1,"rate_version":1}'

echo "-- 计划保养核对：提交计划单（多窗口、非整点排序），期间 10/1 全天 --"
req POST /devices/CNC-01/maintenance-plans '{"device_id":"CNC-01","plan_no":"MP-SMOKE-001","start":"2026-10-01T00:00:00+08:00","end":"2026-10-02T00:00:00+08:00","windows":[
  {"start":"2026-10-01T11:00:00+08:00","end":"2026-10-01T12:00:00+08:00"},
  {"start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T11:00:00+08:00"}]}'

echo "-- 同单号同内容重试（窗口换序、等价 UTC 时区）：预期 200 idempotent=true --"
req POST /devices/CNC-01/maintenance-plans '{"device_id":"CNC-01","plan_no":"MP-SMOKE-001","start":"2026-09-30T16:00:00Z","end":"2026-10-01T16:00:00Z","windows":[
  {"start":"2026-10-01T02:00:00Z","end":"2026-10-01T03:00:00Z"},
  {"start":"2026-10-01T03:00:00Z","end":"2026-10-01T04:00:00Z"}]}'

echo "-- 按单号读取 / 列出设备计划单 --"
req GET /maintenance-plans/MP-SMOKE-001
req GET /devices/CNC-01/maintenance-plans

echo "-- 核对（seq 缺省固定查询开始时最新序号；窗口 10-11 与 11-12 的实际执行看各修订）--"
req GET /maintenance-plans/MP-SMOKE-001/check

echo "-- 固定 seq=1 重放（只取序号不大于 1 的最后修订及固化归因段，结果不可变）--"
req GET /maintenance-plans/MP-SMOKE-001/check?seq=1

echo "-- 核对错误：序号越界 422 INVALID_SEQ / 未知单号 404 MAINTENANCE_PLAN_NOT_FOUND --"
req GET /maintenance-plans/MP-SMOKE-001/check?seq=999999
req GET /maintenance-plans/NOPE/check

echo "-- 提交错误：同单号异内容 409 MAINTENANCE_PLAN_CONFLICT / 期间重叠 409 MAINTENANCE_PLAN_OVERLAP / 窗口越界 422 MAINTENANCE_WINDOW_INVALID --"
req POST /devices/CNC-01/maintenance-plans '{"device_id":"CNC-01","plan_no":"MP-SMOKE-001","start":"2026-10-01T00:00:00+08:00","end":"2026-10-02T00:00:00+08:00","windows":[
  {"start":"2026-10-01T10:00:00+08:00","end":"2026-10-01T10:30:00+08:00"}]}'
req POST /devices/CNC-01/maintenance-plans '{"device_id":"CNC-01","plan_no":"MP-SMOKE-002","start":"2026-10-01T08:00:00+08:00","end":"2026-10-01T20:00:00+08:00","windows":[
  {"start":"2026-10-01T09:00:00+08:00","end":"2026-10-01T09:30:00+08:00"}]}'
req POST /devices/CNC-01/maintenance-plans '{"device_id":"CNC-01","plan_no":"MP-SMOKE-003","start":"2026-10-03T00:00:00+08:00","end":"2026-10-04T00:00:00+08:00","windows":[
  {"start":"2026-10-02T23:00:00+08:00","end":"2026-10-03T01:00:00+08:00"}]}'
