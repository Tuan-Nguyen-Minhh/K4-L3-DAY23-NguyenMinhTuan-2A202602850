"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
EDGE = "http://127.0.0.1:8080"
MEASURE_CMD = ("python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
               "--target-rto 300")


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
           "step": n, "name": name, **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("RUNBOOK", json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N. Đừng bỏ hàm này đi."""
    if auto:
        return True
    print(f"CONFIRM: {msg}")
    try:
        return input("tiep tuc failover? [y/N]: ").strip().lower().startswith("y")
    except EOFError:
        return False


def _last_kill_ts():
    f = pathlib.Path("chaos/chaos-events.jsonl")
    if not f.exists():
        return None
    ts = None
    for line in f.read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("action") == "kill":
            ts = e["ts"]
    return ts


def _health_detected(region: str, since_ts: float, deadline: float):
    """Đọc health-events.jsonl, trả về event UNHEALTHY cho region nếu có."""
    f = pathlib.Path("reports/health-events.jsonl")
    if not f.exists():
        return None
    for line in f.read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (e.get("event") == "state_change" and e.get("region") == region
                and e.get("to") == "UNHEALTHY" and e.get("ts", 0) >= since_ts):
            return e
    return None


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước runbook §4. Bán tự động: confirm trước khi failover trừ khi --auto."""
    t0 = time.time()

    # 1 xac_nhan_outage — chờ health checker log UNHEALTHY cho primary.
    # Runbook đại diện operator: đợi alert từ health checker RỒI mới cutover,
    # không race với nó. Nếu health checker không chạy, fallback tự poll.
    t_outage = _last_kill_ts()
    since = t_outage or (time.time() - 120)
    deadline = time.time() + 70
    detected = None
    probe_reasons = []
    while time.time() < deadline:
        ready, reason = hc.probe(primary, 2.0)
        if not ready:
            probe_reasons.append(reason)
            detected = _health_detected(primary, since, deadline)
            if detected:
                break
        time.sleep(2)
    primary_down = detected is not None or (len(probe_reasons) >= 3
                 and all(not hc.probe(primary, 1.0)[0] for _ in range(3)))
    tgt_ready, _ = hc.probe(target, 2.0)
    step(1, "xac_nhan_outage", primary=primary, target=target,
         probe_reasons=probe_reasons[-3:], primary_down=primary_down,
         target_ready=tgt_ready, health_detected=detected is not None)
    if not primary_down:
        step(7, "post_incident", ok=False, reason="primary_looks_healthy",
             elapsed_s=round(time.time() - t0, 2))
        return {"ok": False, "reason": "primary_looks_healthy"}

    # 2 thong_bao_incident — mốc "operator biết tin", luôn sau t_outage
    t_inc = time.time()
    t_outage = _last_kill_ts()
    step(2, "thong_bao_incident", primary=primary, target=target,
         t_outage=t_outage, t_notify=t_inc,
         notify_delay_s=None if t_outage is None else round(t_inc - t_outage, 2))

    if not confirm(auto, f"Region {primary} DOWN — cutover sang {target}?"):
        step(7, "post_incident", ok=False, reason="operator_declined",
             elapsed_s=round(time.time() - t0, 2))
        return {"ok": False, "reason": "operator_declined"}

    # 3 scale_gpu_pool — failover MỘT LẦN DUY NHẤT (tự làm đủ 5 bước con)
    fo_res = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", ok=fo_res.get("ok"),
         steps_done=fo_res.get("steps_done"), waited_s=fo_res.get("waited_s"))
    if not fo_res.get("ok"):
        step(7, "post_incident", ok=False, reason="failover_aborted",
             elapsed_s=round(time.time() - t0, 2))
        return {"ok": False, "reason": "failover_aborted", "failover": fo_res}

    # 4 verify_state_replica — chỉ ĐỌC kết quả từ dict bước 3, không gọi lại failover
    st = fo_res.get("state") or {}
    step(4, "verify_state_replica", target=target,
         vectors_count=st.get("count"), weights=st.get("weights"),
         pool_state=st.get("pool_state"), rpo_seconds=fo_res.get("rpo_seconds"),
         docs_lost=fo_res.get("docs_lost"),
         embed_model_version=fo_res.get("embed_model_version"))

    # 5 dns_cutover — đọc lại kết quả cutover
    active = pathlib.Path("edge/active_region").read_text().strip()
    cutover_ok = ("5_dns_cutover" in fo_res.get("steps_done", [])
                  and active == target)
    step(5, "dns_cutover", ok=cutover_ok, active_region=active)
    if not cutover_ok:
        step(7, "post_incident", ok=False, reason="cutover_failed",
             elapsed_s=round(time.time() - t0, 2))
        return {"ok": False, "reason": "cutover_failed", "failover": fo_res}

    # 6 verify_golden_signals — 10 request thật qua edge
    # Đợi TTL cache edge (EDGE_TTL_SECONDS=5) hết hạn: không thì request đầu vẫn
    # route về region chết theo cache cũ và golden check fail oan.
    time.sleep(6)
    lat, errs = [], 0
    for i in range(10):
        t = time.time()
        try:
            r = httpx.get(f"{EDGE}/v1/infer",
                          params={"q": f"hoa don thang {i % 12 + 1}"}, timeout=3.0)
            ok = r.status_code == 200 and r.json().get("region") == target
        except Exception:
            ok = False
        if not ok:
            errs += 1
        lat.append(round((time.time() - t) * 1000, 1))
    lat.sort()
    p95 = lat[min(len(lat) - 1, 9)]  # nearest-rank p95 của 10 mẫu
    golden_ok = errs / 10 < 0.10 and p95 < 500
    step(6, "verify_golden_signals", n_requests=10, errors=errs,
         error_rate=round(errs / 10, 2), p95_ms=p95, latencies_ms=lat, ok=golden_ok)

    # 7 post_incident — elapsed_s + lệnh đo RTO
    elapsed = round(time.time() - t_inc, 2)
    step(7, "post_incident", ok=golden_ok, elapsed_s=elapsed,
         total_run_s=round(time.time() - t0, 2), measure_cmd=MEASURE_CMD)
    return {"ok": golden_ok, "target": target, "golden": {"p95_ms": p95, "errors": errs},
            "failover": fo_res, "elapsed_s": elapsed, "measure_cmd": MEASURE_CMD}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
