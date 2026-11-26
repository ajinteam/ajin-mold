import os
import sys

# 프로젝트 루트(/var/task)를 sys.path에 추가하여 app/main.py 및 templates/ 경로 인식 보장
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app.main import app