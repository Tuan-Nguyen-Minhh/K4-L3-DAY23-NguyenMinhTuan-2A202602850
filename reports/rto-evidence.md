# RTO/RPO Evidence — Lab 23 (Drill 1 + Drill 2 đã điền bằng số thật)

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.
Con số không có evidence = trượt, bất kể các phần khác.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-09T04:09:04` | chaos kill (`mode:netblock`, `backend:bare`, `mock:true`) | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+2.0s` | dòng `ok:false` đầu tiên có `ts >= t_outage` — `error:ReadTimeout`, `latency_ms:2023` (edge timeout 2s) | `reports/drill-1-nodr.jsonl:19` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage | `reports/measure-drill-1.json` |
| RTO | `NO_RECOVERY` (15/33 request fail) | `tools/measure_rto.py` | `reports/measure-drill-1.json` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 | `action:kill` (`mode:netblock`, `backend:bare`, `mock:true`) | `chaos/chaos-events.jsonl:9` |
| User thấy lỗi đầu tiên | `+0.0s` | dòng `ok:false` đầu sau t_outage — `error:ReadTimeout` | `reports/drill-2-withdr.jsonl:27` |
| Health check phát hiện | `+18.8s` | `to:UNHEALTHY, region:a` — 3 consecutive fail (interval=5s, threshold=3, floor=15s + 3.8s probe timeout) | `reports/health-events.jsonl:2` |
| Snapshot restore xong | `+20.2s` | `step:2_restore_snapshot` — `rpo_seconds:8.0`, `docs_lost:4` | `reports/failover-events.jsonl:2` |
| Region phụ ready | `+26.6s` | `step:4_wait_ready` — `waited_s:6.34` (GPU warm-up 6s) | `reports/failover-events.jsonl:4` |
| DNS cutover | `+26.6s` | `step:5_dns_cutover` — `from:a → to:b` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **`+27.0s`** | dòng `ok:true` đầu sau lỗi — `served_by:b` | `reports/drill-2-withdr.jsonl:40` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `27.0s` | 300s (5 phút) | PASS |
| RPO — Vector DB | `8.0s` / `4` doc | 300s (5 phút) | PASS |

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | `18.8s` | `interval_s=5 × threshold=3` = 15s floor, +3.8s probe timeout (netblock → ReadTimeout 2s × 3) trong `reports/health-events.jsonl:2` | Hạ `--interval` (1–2s) hoặc `--threshold` 2 — đổi lại nguy cơ flapping khi network lag thoáng qua (§4 Anti-Patterns) |
| Snapshot restore | `1.4s` | t_restore(+20.2s) − t_detect(+18.8s) — `step:2_restore_snapshot` trong `reports/failover-events.jsonl:2` | Replication dày hơn (`--every` nhỏ hơn) để RPO nhỏ, snapshot incremental, DB nhỏ |
| GPU pool warm-up | `6.3s` | `waited_s:6.34` ở `step:4_wait_ready` trong `reports/failover-events.jsonl:4` | Pre-warm: giữ `pool_state=full` thường trực ở region phụ — đổi lại tốn tài nguyên GPU cả ngày |
| DNS/LB TTL cache | `0.4s` | t_recovered(+27.0s) − t_cutover(+26.6s) | Hạ `EDGE_TTL_SECONDS` (5s → 1s) — đổi lại đọc file active_region thường xuyên hơn |
