import os
import sys
import glob
import argparse
import time
from datetime import datetime

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

def list_logs(limit=20):
    files = sorted(glob.glob(os.path.join(LOG_DIR, "survey_run_*.log")), reverse=True)
    if not files:
        print("No logs found.")
        return
    print(f"{'#':<4} {'Filename':<35} {'Size':>10}")
    print("-" * 55)
    for i, f in enumerate(files[:limit], 1):
        size = os.path.getsize(f)
        print(f"{i:<4} {os.path.basename(f):<35} {size/1024:>6.1f} KB")

def tail(path, n=50):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for line in lines[-n:]:
            print(line, end="")
    except Exception as e:
        print(f"Failed to read log: {e}")

def watch(path=None, poll=1.0):
    if path is None:
        path = get_latest()
    print(f"Watching {os.path.basename(path)} ... Ctrl+C to stop")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, os.SEEK_END)
            while True:
                line = f.readline()
                if line:
                    print(line, end="")
                else:
                    time.sleep(poll)
    except KeyboardInterrupt:
        print("\nStopped watching.")
    except Exception as e:
        print(f"Watch failed: {e}")

def new_log():
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(LOG_DIR, f"survey_run_{ts}.log")
    open(path, "a", encoding="utf-8").close()
    print(f"Created {os.path.basename(path)}")

def view(index, n=100):
    files = sorted(glob.glob(os.path.join(LOG_DIR, "survey_run_*.log")), reverse=True)
    if not files:
        print("No logs found.")
        return
    if index < 1 or index > len(files):
        print(f"Invalid index. Use 1-{len(files)}")
        return
    target = files[index - 1]
    print(f"=== {os.path.basename(target)} ===")
    tail(target, n)

def search(keyword, latest_only=True, n=20):
    files = sorted(glob.glob(os.path.join(LOG_DIR, "survey_run_*.log")), reverse=True)
    if latest_only:
        files = files[:1]
    found = 0
    for f in files:
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    if keyword.lower() in line.lower():
                        print(f"{os.path.basename(f)}:{lineno}: {line.rstrip()}")
                        found += 1
                        if found >= n:
                            return
        except Exception:
            pass
    if found == 0:
        print("No matches.")

def stats():
    files = sorted(glob.glob(os.path.join(LOG_DIR, "survey_run_*.log")), reverse=True)
    if not files:
        print("No logs found.")
        return
    total_size = sum(os.path.getsize(f) for f in files)
    print(f"Total logs : {len(files)}")
    print(f"Total size : {total_size/1024/1024:.1f} MB")
    print(f"Latest     : {os.path.basename(files[0])}")

def main():
    parser = argparse.ArgumentParser(description="Sentinel log CLI")
    sub = parser.add_subparsers(dest="cmd")

    p_list = sub.add_parser("list", help="List recent logs")
    p_list.add_argument("-n", type=int, default=20, help="Max entries")

    p_tail = sub.add_parser("tail", help="Tail latest log")
    p_tail.add_argument("-n", type=int, default=50, help="Lines")

    p_view = sub.add_parser("view", help="View log by index")
    p_view.add_argument("index", type=int, help="Log index from list")
    p_view.add_argument("-n", type=int, default=100, help="Lines")

    p_find = sub.add_parser("find", help="Search logs")
    p_find.add_argument("keyword")
    p_find.add_argument("--all", action="store_true", help="Search all logs")

    p_stats = sub.add_parser("stats", help="Log statistics")

    p_watch = sub.add_parser("watch", help="Realtime watch latest log")
    p_watch.add_argument("--file", help="Watch specific log file")
    p_watch.add_argument("--poll", type=float, default=1.0, help="Poll interval seconds")

    p_new = sub.add_parser("new", help="Create a new empty log file")

    args = parser.parse_args()
    if args.cmd == "list":
        list_logs(args.n)
    elif args.cmd == "tail":
        tail(get_latest(), args.n)
    elif args.cmd == "view":
        view(args.index, args.n)
    elif args.cmd == "find":
        search(args.keyword, latest_only=not args.all)
    elif args.cmd == "stats":
        stats()
    elif args.cmd == "watch":
        watch(args.file, args.poll)
    elif args.cmd == "new":
        new_log()
    else:
        parser.print_help()

def get_latest():
    files = sorted(glob.glob(os.path.join(LOG_DIR, "survey_run_*.log")), reverse=True)
    if not files:
        print("No logs found.")
        sys.exit(1)
    return files[0]

if __name__ == "__main__":
    main()
