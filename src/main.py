"""
CLI Entry Point for the Indeed Easy Apply Bot.

Commands:
  run       — Start the bot (search + apply)
  login     — Login to Indeed and save session
  search    — Search and list jobs without applying
  status    — Show statistics from the database
  dashboard — Launch the web dashboard
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from rich.console import Console

console = Console()


def main():
    """Parse CLI arguments and dispatch to the appropriate command."""
    parser = argparse.ArgumentParser(
        prog="indeed-bot",
        description="Indeed Easy Apply Bot — Automate your job applications",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.main run                     # Run in default mode (from config)
  python -m src.main run --mode auto         # Run in full-auto mode
  python -m src.main run --mode semi         # Run in semi-auto mode
  python -m src.main login                   # Login and save session
  python -m src.main search                  # Search and list jobs
  python -m src.main search -q "Data Engineer"
  python -m src.main status                  # Show stats
  python -m src.main dashboard               # Launch web dashboard
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- run ---
    run_parser = subparsers.add_parser("run", help="Start applying to jobs")
    run_parser.add_argument(
        "--mode", choices=["semi", "auto"], default=None,
        help="Application mode: 'semi' (review each) or 'auto' (apply all)"
    )
    run_parser.add_argument(
        "--config", default="config/config.yaml",
        help="Path to config file (default: config/config.yaml)"
    )

    # --- login ---
    login_parser = subparsers.add_parser("login", help="Login to Indeed and save session")
    login_parser.add_argument(
        "--config", default="config/config.yaml",
        help="Path to config file"
    )

    # --- search ---
    search_parser = subparsers.add_parser("search", help="Search and display jobs")
    search_parser.add_argument(
        "-q", "--query", default=None,
        help="Search query (overrides config)"
    )
    search_parser.add_argument(
        "--config", default="config/config.yaml",
        help="Path to config file"
    )

    # --- status ---
    status_parser = subparsers.add_parser("status", help="Show bot statistics")
    status_parser.add_argument(
        "--config", default="config/config.yaml",
        help="Path to config file"
    )

    # --- dashboard ---
    dashboard_parser = subparsers.add_parser("dashboard", help="Launch web dashboard")
    dashboard_parser.add_argument(
        "--port", type=int, default=5000,
        help="Port to run dashboard on (default: 5000)"
    )
    dashboard_parser.add_argument(
        "--host", default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1)"
    )

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    # Dispatch to the right command
    if args.command == "run":
        _cmd_run(args)
    elif args.command == "login":
        _cmd_login(args)
    elif args.command == "search":
        _cmd_search(args)
    elif args.command == "status":
        _cmd_status(args)
    elif args.command == "dashboard":
        _cmd_dashboard(args)


# ------------------------------------------------------------------
# Command Implementations
# ------------------------------------------------------------------

def _cmd_run(args):
    """Start the bot."""
    from src.bot import IndeedBot

    console.print("[bold cyan]🚀 Indeed Easy Apply Bot[/bold cyan]\n")
    bot = IndeedBot(config_path=args.config)
    try:
        asyncio.run(bot.run(mode=args.mode))
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped by user[/yellow]")


def _cmd_login(args):
    """Login and save session."""
    from src.bot import IndeedBot

    console.print("[bold cyan]🔑 Indeed Login[/bold cyan]\n")
    bot = IndeedBot(config_path=args.config)
    asyncio.run(bot.login_only())


def _cmd_search(args):
    """Search and display results."""
    from src.bot import IndeedBot

    console.print("[bold cyan]🔍 Indeed Job Search[/bold cyan]\n")
    bot = IndeedBot(config_path=args.config)
    asyncio.run(bot.search_only(query=args.query))


def _cmd_status(args):
    """Show statistics."""
    from src.bot import IndeedBot

    bot = IndeedBot(config_path=args.config)
    asyncio.run(bot.show_status())


def _cmd_dashboard(args):
    """Launch the web dashboard."""
    from src.dashboard.app import create_app

    console.print(
        f"[bold cyan]📊 Launching Dashboard[/bold cyan] at "
        f"[link=http://{args.host}:{args.port}]http://{args.host}:{args.port}[/link]\n"
    )

    app = create_app(db_path="data/indeed_bot.db")
    app.run(host=args.host, port=args.port, debug=True)


if __name__ == "__main__":
    main()
