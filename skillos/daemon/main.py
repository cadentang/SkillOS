"""daemon 主循环。实现待 P2（04 §2.5.2）。"""

from __future__ import annotations


def main(cloud_url: str | None = None, pair_code: str | None = None) -> None:
    raise NotImplementedError("P2 实现：WS 客户端 + 任务循环 + watchdog")


if __name__ == "__main__":
    main()
