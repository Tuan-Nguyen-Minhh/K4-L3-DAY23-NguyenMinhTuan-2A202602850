"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **kw}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("FAILOVER", json.dumps(rec))
    return rec


def state_of(region: str) -> dict:
    """/v1/state của 1 region — helper cho 1_verify_target (best-effort, không abort)."""
    try:
        return httpx.get(f"{URL[region]}/v1/state", timeout=2.0).json()
    except Exception as e:
        return {"region": region, "reachable": False, "error": type(e).__name__}


def failover(target: str, backend: str, wait: float) -> dict:
    """5 bước failover, đúng thứ tự. Bước 4 timeout -> ABORT, không cutover."""
    primary = "a" if target == "b" else "b"
    done = []

    st = state_of(target)
    emit(step="1_verify_target", region=target, state=st)
    done.append("1_verify_target")

    meta = snapshot.get(target, backend)
    rpo = snapshot.rpo(pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
                       pathlib.Path(f"state/region-{target}/vectors.sqlite"))
    emit(step="2_restore_snapshot", target=target, backend=backend,
         rpo_seconds=rpo["rpo_seconds"], docs_lost=rpo["docs_lost"],
         embed_model_version=meta.get("embed_model_version"),
         snapshot_at=meta.get("snapshot_at"), restored_at=meta.get("restored_at"))
    done.append("2_restore_snapshot")

    pathlib.Path(f"state/region-{target}/pool_state").write_text("full")
    emit(step="3_scale_pool", region=target, pool_state="full")
    done.append("3_scale_pool")

    t0, deadline = time.time(), time.time() + wait
    ready = False
    while time.time() < deadline:
        try:
            if httpx.get(f"{URL[target]}/readyz", timeout=2.0).status_code == 200:
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.25)
    waited = round(time.time() - t0, 2)
    if not ready:
        emit(step="4_wait_ready", region=target, ok=False,
             reason="timeout_readyz", waited_s=waited)
        return {"ok": False, "target": target, "reason": "target_not_ready",
                "steps_done": done, "waited_s": waited,
                "rpo_seconds": rpo["rpo_seconds"], "docs_lost": rpo["docs_lost"]}
    emit(step="4_wait_ready", region=target, ok=True, waited_s=waited)
    done.append("4_wait_ready")

    pathlib.Path("edge/active_region").write_text(target)
    emit(step="5_dns_cutover", from_region=primary, to_region=target)
    done.append("5_dns_cutover")
    return {"ok": True, "target": target, "steps_done": done, "waited_s": waited,
            "rpo_seconds": rpo["rpo_seconds"], "docs_lost": rpo["docs_lost"],
            "embed_model_version": meta.get("embed_model_version"),
            "state": state_of(target)}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
