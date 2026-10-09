# Postmortem — DR Drill Lab 23

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T05:40:57 | outage bắt đầu — kill region A (netblock/SIGSTOP) | `chaos/chaos-events.jsonl:9` |
| 2026-10-09T05:40:57 | user đầu tiên bị ảnh hưởng — `ReadTimeout` +0.0s sau t_outage | `reports/drill-2-withdr.jsonl:27` |
| 2026-10-09T05:41:16 | health check alert — region A `UNHEALTHY` sau 3 consecutive fail (+18.8s) | `reports/health-events.jsonl:2` |
| 2026-10-09T05:41:17 | operator confirm cutover — runbook step 2, notify_delay +20.0s | `reports/runbook-run.jsonl:2` |
| 2026-10-09T05:41:23 | resolved — request đầu tiên OK từ region B (+27.0s) | `reports/drill-2-withdr.jsonl:40` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `27.0s` · gap: `273.0s` (dư mục tiêu 10x)
- RPO mục tiêu: 300s · đo được: `8.0s` (`4` doc bị mất) · gap: `292.0s` (dư mục tiêu 37x)
- **Bước tốn nhiều giây nhất:** `Health-check detect floor` (18.8s / 27.0s = 70% RTO) —
  vì `interval=5s × threshold=3` = 15s floor là tối thiểu, cộng thêm 3.8s probe timeout
  do netblock (SIGSTOP) khiến mỗi probe `/readyz` treo 2s trước khi timeout.

## 3. Root cause (5 whys)

Không phải "vì tôi chạy chaos script". Câu hỏi: *nếu đây là outage thật, bước nào
trong runbook của tôi sẽ thất bại?*

Drill 1 (không DR, `chaos/chaos-events.jsonl:1`, 2026-10-09T04:09:04) chứng minh chuỗi
nguyên nhân sau: kill Region A (netblock) → 15/33 request fail, không bao giờ phục hồi
(`reports/measure-drill-1.json`: `NO_RECOVERY`).

1. Vì sao user thấy lỗi mãi? → Không component nào phát hiện region A chết và
   chuyển traffic. Chỉ có user phát hiện — qua chính request lỗi của họ.
2. Vì sao không ai phát hiện? → Chưa có health checker. Cách duy nhất đang có là
   `/healthz` (liveness) — nó vẫn trả `alive:true` khi vector DB rỗng, tức process
   sống ≠ region serve được (`serving/app.py:56-59`).
3. Vì sao không thể chuyển sang Region B? → B rỗng: `count:0`, `weights:false`,
   `pool_state:warm` (`localhost:8002/v1/state` lúc baseline). Cutover sớm chỉ tạo
   503 từ CẢ HAI phía.
4. Vì sao B rỗng? → Không có lịch replication — chưa từng chạy `state/replicate.py`,
   chưa từng có snapshot (`state/_replica/` không tồn tại ở fresh checkout).
5. **Root cause:** hệ thống thiếu CẢ hai tầng phòng thủ độc lập — detection
   (health check trên `/readyz`) và state replication (snapshot định kỳ). Thiếu một
   trong hai thì RTO vẫn vô hạn; thiếu cả hai thì outage là vĩnh viễn.

Nếu là outage thật, bước sẽ thất bại trong runbook: **bước 3 (restore)** — nếu
`state/replicate.py` không chạy định kỳ thì không có snapshot gần, restore chết hoặc
RPO phình to; và **bước 5 (cutover)** nếu không có bước 4 chặn — cutover sớm = 503 hai phía.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Health checker poll `/readyz` cả 2 region, threshold 3 lần liên tiếp, log `interval_s`+`threshold` | SRE on-call | Đã xong (Step 3a, `dr/health_checker.py`) | Phát hiện trong 15s (detect floor 5×3) thay vì vô hạn |
| 2 | Failover 5 bước có thứ tự, ABORT nếu target chưa ready | SRE on-call | Đã xong (Step 3b, `dr/failover.py`) | Loại bỏ cutover-sớm (503 hai phía); RTO = floor + restore + warmup + TTL |
| 3 | `state/replicate.py --every 30 --backend fs` chạy TRƯỚC drill, đủ ≥1 chu kỳ | DBA | Trước drill 2 | RPO ≤ ~30s thay vì mất toàn bộ dữ liệu mới |
| 4 | Runbook semi-auto (y/N confirm, không full-auto) | on-call | Đã xong (Step 3c, `dr/runbook.py`) | Giảm sai sót vận hành lúc 3h sáng; chống flapping |

## 5. Ba câu hỏi bắt buộc trả lời

1. `interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?
   → 5s × 3 = **15 giây** floor. Thực tế detection mất **18.8s** (thêm 3.8s do probe
   timeout trên netblock). Chiếm **70%** RTO (18.8s / 27.0s) — thành phần lớn nhất.
2. Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?
   → Floor giảm còn 1×3 = 3s → tiết kiệm 12s. Giá phải trả: poll dày gấp 5 lần,
   một lag mạng thoáng qua cũng tích consecutive fail nhanh hơn → nguy cơ flip
   qua lại giữa 2 region (flapping) tăng; muốn giữ an toàn thì phải giảm threshold
   (nguy hiểm hơn) hoặc chấp nhận false positive. 12s đổi lấy rủi ro đó không đáng
   khi RTO mục tiêu là 300s.
3. Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost`
   của bạn có nghĩa gì với khách hàng?
   → Mỗi doc là một ticket khách hàng (câu hỏi về hóa đơn). `docs_lost` = số câu
   hỏi khách hỏi trong khoảng replication lag mà hệ thống KHÔNG còn kiến thức để
   trả lời — kể cả khi region phụ lên, nó trả lời bằng data cũ/sai. Drill 2 đo được
   `docs_lost:4` (`reports/failover-events.jsonl:2`) với RPO 8.0s. Nếu outage 6 giờ
   mà replicate mỗi 30s, `docs_lost` có thể lên tới ~720 doc (6h × 0.5 doc/s) —
   720 ticket khách hàng mất câu trả lời vĩnh viễn.
