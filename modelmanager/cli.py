from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

DEFAULT_URL = os.environ.get("MODELMANAGER_URL", "http://127.0.0.1:8778")


def _client(args) -> httpx.Client:
    return httpx.Client(base_url=args.url, timeout=30)


def _print_response(resp: httpx.Response) -> int:
    try:
        body = resp.json()
    except ValueError:
        body = {"error": resp.text}
    if resp.status_code >= 400 and "error" not in body and "detail" in body:
        body = {"error": body["detail"]}
    print(json.dumps(body))
    return 0 if resp.status_code < 400 else 1


def cmd_serve(args) -> int:
    import uvicorn

    uvicorn.run("modelmanager.coordinator.app:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def cmd_list(args) -> int:
    with _client(args) as client:
        return _print_response(client.get("/v1/models"))


def cmd_ensure(args) -> int:
    with _client(args) as client:
        return _print_response(client.post(f"/v1/models/{args.name}/ensure"))


def cmd_stop(args) -> int:
    with _client(args) as client:
        return _print_response(client.post(f"/v1/models/{args.name}/stop"))


def cmd_logs(args) -> int:
    with _client(args) as client:
        resp = client.get(f"/v1/models/{args.name}/logs", params={"lines": args.lines})
        if resp.status_code >= 400:
            return _print_response(resp)
        print(resp.json().get("logs", ""))
        return 0


def cmd_stats(args) -> int:
    with _client(args) as client:
        return _print_response(client.get(f"/v1/systems/{args.system}/stats"))


def cmd_systems(args) -> int:
    with _client(args) as client:
        return _print_response(client.get("/v1/systems"))


def cmd_summary(args) -> int:
    with _client(args) as client:
        return _print_response(client.get("/v1/status/summary"))


def main() -> None:
    parser = argparse.ArgumentParser(prog="modelmanager")
    parser.add_argument("--url", default=DEFAULT_URL, help="coordinator base URL")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="run the coordinator REST service")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8778)
    p_serve.add_argument("--reload", action="store_true")
    p_serve.set_defaults(func=cmd_serve)

    sub.add_parser("list", help="list model definitions and status").set_defaults(func=cmd_list)
    sub.add_parser("summary", help="compact running-model summary").set_defaults(func=cmd_summary)
    sub.add_parser("systems", help="list configured systems").set_defaults(func=cmd_systems)

    p_ensure = sub.add_parser("ensure", help="ensure a model is loaded")
    p_ensure.add_argument("name")
    p_ensure.set_defaults(func=cmd_ensure)

    p_stop = sub.add_parser("stop", help="stop a running model")
    p_stop.add_argument("name")
    p_stop.set_defaults(func=cmd_stop)

    p_logs = sub.add_parser("logs", help="show recent logs for a model")
    p_logs.add_argument("name")
    p_logs.add_argument("--lines", type=int, default=200)
    p_logs.set_defaults(func=cmd_logs)

    p_stats = sub.add_parser("stats", help="show GPU/VRAM stats for a system")
    p_stats.add_argument("system")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
