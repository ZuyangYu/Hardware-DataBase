"""蹲守 zen 免费额度窗口: 限流解除后自动跑 5 题 SQL 路由探测."""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(REPO)

from openai import OpenAI  # noqa: E402

from src.settings import AGENT_CUSTOM_API_KEY, AGENT_CUSTOM_BASE_URL, AGENT_CUSTOM_MODEL  # noqa: E402

client = OpenAI(base_url=AGENT_CUSTOM_BASE_URL, api_key=AGENT_CUSTOM_API_KEY)

while True:
    try:
        client.chat.completions.create(
            model=AGENT_CUSTOM_MODEL, messages=[{"role": "user", "content": "hi"}], max_tokens=5
        )
        print(f"[{time.strftime('%H:%M:%S')}] {AGENT_CUSTOM_MODEL} 可用, 启动探测", flush=True)
        break
    except Exception as exc:
        tag = "限流" if "429" in str(exc) else "其他错误"
        print(f"[{time.strftime('%H:%M:%S')}] {tag}, 5 分钟后重试", flush=True)
        time.sleep(300)

env = dict(os.environ)
env["AGENT_CUSTOM_MODEL"] = AGENT_CUSTOM_MODEL
subprocess.run(
    [
        ".venv/bin/python", "-u", "benchmarks/spreadsheet/run_answer_eval.py",
        "--arm", "b", "--ids", "AG01,AG02,AG04,CP03,CP05", "--workers", "1",
    ],
    env=env,
)
