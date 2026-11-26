import os
import urllib.request

APP_URL = os.getenv("APP_URL", "http://localhost:8000").rstrip("/")

with urllib.request.urlopen(f"{APP_URL}/api/health", timeout=15) as resp:
    print("Ping status:", resp.status, resp.read().decode("utf-8"))