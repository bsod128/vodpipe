"""E2E: submit -> cancel mid-ladder -> expect CANCELLED with partial rungs + report."""
import json
import sys
import time
import urllib.error
import urllib.request

API = "http://127.0.0.1:8000"


def req(method, path):
    r = urllib.request.Request(API + path, method=method)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
    return json.loads(raw)


def submit(body):
    data = json.dumps(body).encode()
    r = urllib.request.Request(API + "/jobs", data=data, method="POST",
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def main():
    rungs = (sys.argv[1] if len(sys.argv) > 1 else "2160p,1440p,1080p,720p,432p").split(",")
    stop_at = sys.argv[2] if len(sys.argv) > 2 else "2/"
    code, job = submit({
        "source": "src_clip/4k_master.mp4",
        "codecs": ["h264"],
        "rungs": rungs,
        "metrics": False,
        "watermark": {"text": "CANCEL-TEST", "opacity": 0.2},
    })
    print("submit:", code, job.get("state"), flush=True)
    if "job_id" not in job:
        print(json.dumps(job, indent=2, ensure_ascii=False))
        return 1
    jid = job["job_id"]
    print("job:", jid, flush=True)

    seen = None
    cancelled = False
    deadline = time.time() + 420
    while time.time() < deadline:
        st = req("GET", f"/jobs/{jid}")
        sig = (st.get("state"), st.get("stage"))
        if sig != seen:
            seen = sig
            print(f"  {st.get('state')} p={st.get('progress')}: {st.get('stage')}", flush=True)
        stage = st.get("stage") or ""
        if not cancelled and stage.startswith("encode") and stop_at in stage:
            print(f"  -> cancel at {stage}", flush=True)
            cr = req("POST", f"/jobs/{jid}/cancel")
            print("  cancel resp state:", cr.get("state"), flush=True)
            cancelled = True
        if st.get("state") in ("SUCCESS", "CANCELLED", "FAILURE"):
            print("\nFINAL:", json.dumps(st, indent=2, ensure_ascii=False))
            try:
                rep = req("GET", f"/jobs/{jid}/report")
            except Exception as e:
                print("report unavailable:", e)
                return 1
            print("report: cancelled =", rep.get("cancelled"),
                  "| rungs:", [r.get("rung") for r in rep.get("encode") or []],
                  "| ladder_wall_s:", rep.get("ladder_wall_s"),
                  "| total_wall_s:", rep.get("total_wall_s"), flush=True)
            ok = st.get("state") == "CANCELLED" and bool(rep.get("cancelled"))
            print("ASSERT partial-cancel report:", "OK" if ok else "FAIL", flush=True)
            return 0 if ok else 1
        time.sleep(2)
    print("timeout waiting for terminal state")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
