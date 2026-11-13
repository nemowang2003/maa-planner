import json
import sys


def test_owned_worker_job_closes_its_entire_process_tree(tmp_path):
    import subprocess

    import psutil

    from maa_planner.daemon.adapters.windows import ProcessJob

    script = """import json,subprocess,sys,time,os,psutil
print(json.dumps({'pid':os.getpid()}), flush=True)
assert sys.stdin.readline().strip() == 'owned'
p = subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
outside = subprocess.Popen([sys._base_executable,'-c','import time; time.sleep(60)'],
                           creationflags=0x01000000, stdout=subprocess.DEVNULL)
print(json.dumps({'child':p.pid, 'outside':outside.pid}), flush=True)
time.sleep(60)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    assert process.stdout is not None and process.stdin is not None
    actual = psutil.Process(json.loads(process.stdout.readline())["pid"])
    job = ProcessJob(actual.pid)
    try:
        process.stdin.write("owned\n")
        process.stdin.flush()
        children = json.loads(process.stdout.readline())
        child = psutil.Process(children["child"])
        outside = psutil.Process(children["outside"])
    finally:
        job.close()
    process.wait(timeout=5)
    actual.wait(timeout=5)
    child.wait(timeout=5)
    try:
        assert outside.is_running()
    finally:
        outside.terminate()
        outside.wait(timeout=5)
    process.stdin.close()
    process.stdout.close()
