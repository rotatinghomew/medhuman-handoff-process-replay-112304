#!/usr/bin/env python3 -I
"""分析报告启动入口：隔离模式运行，显式加入项目路径。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.report import main

if __name__ == "__main__":
    main()
