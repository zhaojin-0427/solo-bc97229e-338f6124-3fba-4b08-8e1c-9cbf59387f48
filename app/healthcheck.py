"""容器健康检查：仅依赖标准库访问 /health。"""
import sys
import urllib.request

try:
    with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2) as resp:
        sys.exit(0 if resp.status == 200 else 1)
except Exception:
    sys.exit(1)
