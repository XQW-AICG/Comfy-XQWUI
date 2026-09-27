"""main —— 引擎服务启动入口。

用法：python -m engine.main  或  python engine/main.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import config as cfg            # noqa: E402
from engine.api import EngineApp            # noqa: E402


def main():
    cfg.ensure_dirs()
    app = EngineApp()
    host = cfg.get_cfg()["host"]
    port = int(cfg.get_cfg()["port"])
    print("=" * 58)
    print("XQWUI 轻量推理引擎")
    print("  地址:    http://%s:%d" % (host if host != "0.0.0.0"
                                        else "127.0.0.1", port))
    print("  规范:    Comfy 后端 API（/prompt /queue /history /ws …）")
    print("  数据:    %s" % cfg.DATA_DIR)
    print("=" * 58)
    try:
        app.serve_forever(host, port)
    except KeyboardInterrupt:
        app.executor.stop()
        print("\n[engine] 已停止")


if __name__ == "__main__":
    main()
