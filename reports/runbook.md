# Runbook 1 trang — Region chính down

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước: lệnh copy-paste
được + cách biết bước đó xong.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python chaos/kill_region.py status` | `a.alive=false` 3 lần liên tiếp | on-call |
| 2 | Mở incident + bấm giờ RTO | `python dr/runbook.py --primary a --target b --backend fs` (trả lời `y` khi được hỏi; `--auto` chỉ dùng cho drill chấm điểm) | dòng `ts` đầu tiên xuất hiện trong `reports/runbook-run.jsonl` | on-call |
| 3 | Restore state ở region phụ | `python state/snapshot.py get --region b --backend fs` | `state/region-b/vectors.sqlite` + `weights/model.bin` mới vừa ghi, `MANIFEST.json` đọc được (`embed_model_version` khớp region A) | on-call |
| 4 | Scale pool warm→full | `printf full > state/region-b/pool_state` | `/readyz` của b trả 200 — kiểm tra: `curl localhost:8002/readyz` | on-call |
| 5 | DNS/LB cutover | `printf b > edge/active_region` | `curl localhost:8080/edge/state` cho `active_region:"b"` | on-call — CHỈ chạy khi bước 4 đã 200 |
| 6 | Verify golden signals | `python loadgen/traffic.py --duration 5 --rps 2 --out reports/golden-signals.jsonl` | p95 < 500ms, error rate < 10% (≤1/10 request `ok:false`) | on-call |
| 7 | Đo RTO + postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `rto_verdict` = `PASS` và `valid: true` | incident commander |

**Rollback (failover ngược):** trả traffic về Region A khi Region B tự hỏng sau cutover
(error rate > 50% trong 60s, hoặc `/readyz` của B trở về 503) VÀ Region A đã sống lại
(`restore --region a --backend bare` xong, `/readyz` của A trả 200). Ai quyết định:
incident commander — không ai khác. Lệnh: `printf a > edge/active_region`, rồi chạy lại
bước 6 để verify golden signals trên A.

> Ghi chú máy Windows (lab này): mọi lệnh `python3`/`printf` chạy qua WSL —
> `bash -c 'source .venv-wsl/bin/activate && <lệnh>'`. Không dùng Start-Job; mỗi
> process một terminal. Chaos chỉ hiệu lực trên PID Linux trong `run/region-*.pid`.
