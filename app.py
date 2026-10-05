import argparse
import signal
import sys
from pathlib import Path

from src.http_api import create_server
from src.maintenance import MaintenanceWorker
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def main(argv=None):
    parser = argparse.ArgumentParser(description="实验室仪器校准与方法验证")
    parser.add_argument("--db", default="./data.db", help="SQLite database path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8309)
    parser.add_argument("--maintenance-interval", type=int, default=300,
                        help="后台依赖重算/旧数据回填轮询秒数，0 表示不启动")
    parser.add_argument("--backfill-batch", type=int, default=100,
                        help="每轮回填的旧结果数量")
    args = parser.parse_args(argv)

    repository = SQLiteRepository(args.db)
    rules = RuleEngine()
    service = DomainService(repository, rules)
    static_dir = Path(__file__).resolve().parent / "static"
    server = create_server(args.host, args.port, service, rules, str(static_dir))

    worker = None
    if args.maintenance_interval > 0:
        worker = MaintenanceWorker(
            service,
            interval_seconds=args.maintenance_interval,
            batch_size=args.backfill_batch,
        )
        worker.start()

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        print("实验室仪器校准与方法验证 listening on http://%s:%s" % (args.host, args.port), flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if worker:
            worker.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
