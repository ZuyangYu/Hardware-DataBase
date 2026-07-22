"""``hardware-database`` CLI entry point.

Subcommands: login / whoami / list-kb / query / upload / list-files / delete.
Authentication prefers ``--token`` / ``HDB_TOKEN``, falling back to the token
saved by ``login``. Query streams the answer to stdout by default; ``--json``
collects the stream and emits a structured result (answer + evidence +
summary) for machine consumers such as Claude Code.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from src.cli import config as cfg
from src.cli import output
from src.cli import session as sess
from src.cli.client import ApiClient, ApiError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hardware-database", description="Hardware DataBase 客户端")
    parser.add_argument("--api-url", default=None, help="API 地址(默认读 HDB_API_URL 或会话文件)")
    parser.add_argument("--token", default=None, help="访问令牌(也可用 HDB_TOKEN 环境变量)")
    parser.add_argument("--json", action="store_true", help="结构化 JSON 输出")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="登录并保存令牌")
    login.add_argument("--user", required=True)
    login.add_argument("--password", default=None)

    sub.add_parser("whoami", help="当前用户信息")

    sub.add_parser("list-kb", help="列出可访问知识库")

    q = sub.add_parser("query", help="检索知识库")
    q.add_argument("--kb", required=True, help="知识库名称")
    q.add_argument("question", help="问题")

    up = sub.add_parser("upload", help="上传文件(部门管理员)")
    up.add_argument("--kb", required=True, help="目标知识库")
    up.add_argument("--group", default=None, help="来源分组(缺省自动分类)")
    up.add_argument("files", nargs="+", help="文件路径")

    lf = sub.add_parser("list-files", help="列出知识库内文件")
    lf.add_argument("--kb", required=True)

    df = sub.add_parser("delete", help="删除文件(需 admin)")
    df.add_argument("--kb", required=True)
    df.add_argument("--file", required=True)

    return parser


def _client(args, require_token: bool = True) -> ApiClient:
    s = sess.load_session() or {}
    token = args.token or os.getenv("HDB_TOKEN") or s.get("token")
    api_url = cfg.resolve_api_url(args.api_url, s.get("api_url"))
    if require_token and not token:
        print("未登录。请先运行 `hardware-database login`。", file=sys.stderr)
        sys.exit(2)
    return ApiClient(api_url, token=token)


def _cmd_login(args, as_json: bool) -> int:
    s = sess.load_session() or {}
    api_url = cfg.resolve_api_url(args.api_url, s.get("api_url"))
    password = args.password or getpass.getpass("密码: ")
    client = ApiClient(api_url)
    res = client.login(args.user, password)
    sess.save_session(res["user"]["username"], res["token"], api_url)
    output.render_user(res["user"], as_json=as_json)
    print(f"已保存登录信息到 {sess.session_path()}", file=sys.stderr)
    return 0


def _cmd_query(args, as_json: bool) -> int:
    client = _client(args)
    if as_json:
        answer_parts: list[str] = []
        summary: dict | None = None
        for event, data in client.query(args.kb, args.question):
            if event == "delta":
                answer_parts.append(data.get("text", ""))
            elif event == "done":
                summary = data
            elif event == "error":
                print(f"错误: {data.get('message')}", file=sys.stderr)
                return 1
        if summary is None:
            summary = {}
        summary["answer"] = "".join(answer_parts)
        output.print_json(summary)
        return 0
    for event, data in client.query(args.kb, args.question):
        if event == "delta":
            print(data.get("text", ""), end="", flush=True)
        elif event == "error":
            print(f"\n错误: {data.get('message')}", file=sys.stderr)
            return 1
    print()
    return 0


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = getattr(args, "json", False)
    try:
        if args.command == "login":
            return _cmd_login(args, as_json)
        client = _client(args)
        if args.command == "whoami":
            output.render_user(client.whoami(), as_json=as_json)
        elif args.command == "list-kb":
            output.render_kbs(client.list_kbs(), as_json=as_json)
        elif args.command == "query":
            return _cmd_query(args, as_json)
        elif args.command == "upload":
            output.render_upload(client.upload(args.kb, args.files, source_group=args.group), as_json=as_json)
        elif args.command == "list-files":
            output.render_files(client.list_files(args.kb), as_json=as_json)
        elif args.command == "delete":
            res = client.delete_file(args.kb, args.file)
            output.print_json(res) if as_json else print(res.get("message", "已删除"))
        return 0
    except ApiError as exc:
        print(f"API 错误: {exc.message}", file=sys.stderr)
        if exc.status_code == 401:
            print("令牌可能过期,请重新 `hardware-database login`。", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"文件不存在: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
