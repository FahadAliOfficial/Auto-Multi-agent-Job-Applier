"""
Bot Orchestrator — The "Brain" of the Indeed Easy Apply Bot.

Coordinates the entire application workflow:
1. Load config
2. Initialize browser (reuse session if available)
3. Login if needed
4. Run search queries
5. For each job: check duplicates → open → review → apply → track
6. Print session summary
"""
from __future__ import annotations

import asyncio
import threading
import webbrowser
import yaml
from pathlib import Path

from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.panel import Panel

from src.browser import BrowserManager
from src.database import Database, Job
from src.pages.login_page import LoginPage
from src.pages.search_page import SearchPage, JobListing
from src.pages.job_page import JobPage
from src.pages.apply_form import ApplyForm
from src.handlers.question_matcher import QuestionMatcher
from src.orchestrator import AgentOrchestrator
from src.control_center import REGISTRY
from src.utils.logger import cc_print, logger, console
from src.utils.delay import between_pages
from src.utils.screenshot import (
    capture_application_screenshot,
    capture_on_error,
)
from src.dashboard.app import create_app


class IndeedBot:
    """Main orchestrator for the Indeed Easy Apply bot."""

    def __init__(self, config_path: str = "config/config.yaml"):
        self.config_path = Path(config_path)
        self.config: dict = {}
        self.db = Database()
        self.browser_manager: BrowserManager | None = None
        self.question_matcher: QuestionMatcher | None = None

        # Session stats
        self.session_id: int = 0
        self.jobs_found: int = 0
        self.jobs_applied: int = 0
        self.jobs_skipped: int = 0
        self.jobs_failed: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def load_config(self) -> dict:
        """Load configuration from YAML file."""
        if not self.config_path.exists():
            logger.error(f"Config file not found: {self.config_path}")
            raise FileNotFoundError(f"Config not found: {self.config_path}")

        with open(self.config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

        logger.info(f"📂 Loaded config from {self.config_path}")
        return self.config

    async def initialize(self) -> None:
        """Initialize all components (database, browser, question matcher)."""
        self.load_config()

        # Database
        await self.db.connect()
        closed = await self.db.close_open_sessions()
        if closed:
            logger.info("Closed %s stale search session(s) from previous runs", closed)
        logger.info("🗄️ Database connected")

        # Question matcher
        self.question_matcher = QuestionMatcher(self.db.conn)

        # Browser
        self.browser_manager = BrowserManager(self.config)

    async def shutdown(self) -> None:
        """Cleanup all resources."""
        if self.browser_manager:
            await self.browser_manager.stop()
        await self.db.close()
        logger.info("🛑 Bot shut down")

    # ------------------------------------------------------------------
    # Main Workflows
    # ------------------------------------------------------------------

    async def run(self, mode: str | None = None, agents_override: int | None = None) -> None:
        """Main workflow: search → filter → apply.

        Args:
            mode: "semi" or "auto". Overrides config if provided.
        """
        try:
            await self.initialize()
            if agents_override is not None:
                self.config.setdefault("bot", {})["agents"] = max(1, int(agents_override))
            effective_mode = mode or self.config.get("bot", {}).get("mode", "semi")
            max_apps = self.config.get("bot", {}).get("max_applications", 25)

            console.print(
                Panel(
                    f"[bold]Indeed Easy Apply Bot[/bold]\n"
                    f"Mode: [cyan]{effective_mode}[/cyan]\n"
                    f"Max applications: [cyan]{max_apps or 'unlimited'}[/cyan]",
                    title="🚀 Starting",
                    border_style="cyan",
                )
            )

            # Start browser
            page = await self.browser_manager.start()
            login_page = LoginPage(page)

            # Login
            if not await login_page.is_logged_in():
                email = self.config.get("personal", {}).get("email", "")
                success = await login_page.login(email)
                if not success:
                    logger.error("Failed to login. Aborting.")
                    return
                await self.browser_manager.save_session()
            else:
                logger.info("♻️ Using existing session")

            # Run search queries
            queries = self.config.get("search", {}).get("queries", [])
            if not queries:
                logger.error("No search queries configured in config.yaml")
                return

            bot_cfg = self.config.get("bot", {})
            agents_count = int(bot_cfg.get("agents", 1))
            orch_cfg = bot_cfg.get("orchestrator", {}) or {}
            orchestrator_enabled = bool(orch_cfg.get("enabled", True))

            # Always start the control center if enabled (regardless of agent count)
            if orch_cfg.get("control_center", True):
                cc_host = str(orch_cfg.get("host", "127.0.0.1"))
                cc_port = int(orch_cfg.get("port", 5000))
                cc_app = create_app(db_path="data/indeed_bot.db")
                server_thread = threading.Thread(
                    target=cc_app.run,
                    kwargs={"host": cc_host, "port": cc_port, "debug": False, "use_reloader": False},
                    daemon=True,
                )
                server_thread.start()
                cc_url = f"http://{cc_host}:{cc_port}/control-center"
                console.print(f"[bold cyan]📊 Control Center:[/bold cyan] {cc_url}")
                # Auto-open the control center in the default browser after a short delay
                threading.Timer(1.5, webbrowser.open, args=[cc_url]).start()

            if orchestrator_enabled and agents_count > 1:
                orchestrator = AgentOrchestrator(self)
                await orchestrator.run(effective_mode, max_apps, page)
                self._print_summary()
                return

            # Single-agent path: register in REGISTRY so the control center
            # shows live state, stats, and routes semi-auto prompts.
            single_agent_id = ""
            if orch_cfg.get("control_center", True):
                single_agent_id = "agent-1"
                REGISTRY.ensure_agent(single_agent_id)
                REGISTRY.set_state(single_agent_id, "idle")
                REGISTRY.append_log(single_agent_id, "single-agent mode started")

            # Outer loop: countries, then queries
            countries = orch_cfg.get("countries", ["us"])
            for country in countries:
                country = country.lower()
                console.print(f"\n[bold magenta]🌍 Searching Indeed [{country.upper()}][/bold magenta]")
                if single_agent_id:
                    REGISTRY.append_log(single_agent_id, f"--- country: {country.upper()} ---")

                for query in queries:
                    if max_apps and self.jobs_applied >= max_apps:
                        console.print(
                            f"[yellow]⚠ Reached max applications limit ({max_apps}). Stopping.[/yellow]"
                        )
                        break

                    await self._run_search_query(
                        page, query, effective_mode, max_apps,
                        agent_id=single_agent_id, country=country,
                    )

                if max_apps and self.jobs_applied >= max_apps:
                    break

            if single_agent_id:
                REGISTRY.set_state(single_agent_id, "done")
                REGISTRY.append_log(single_agent_id, "all queries complete")

            # Print summary
            self._print_summary()

        except KeyboardInterrupt:
            console.print("\n[yellow]⚠ Interrupted by user[/yellow]")
        except Exception as e:
            logger.error(f"Bot error: {e}", exc_info=True)
            if self.browser_manager and self.browser_manager._page:
                await capture_on_error(self.browser_manager.page, e)
        finally:
            await self.shutdown()

    async def _run_search_query(
        self,
        page,
        query: str,
        mode: str,
        max_apps: int,
        agent_id: str = "",
        country: str = "us",
    ) -> None:
        """Run a single search query through multiple pages of results."""
        search_page = SearchPage(page, self.config, country=country)
        job_page = JobPage(page)
        max_pages = self.config.get("search", {}).get("max_pages", 5)

        # Start a search session in DB
        location = self.config.get("search", {}).get("location", "")
        self.session_id = await self.db.start_session(query, location)

        console.print(f"\n[bold cyan]🔍 Search Query: '{query}' [{country.upper()}][/bold cyan]")
        if agent_id:
            REGISTRY.set_state(agent_id, "searching", query=f"{query} [{country.upper()}]")
            REGISTRY.append_log(agent_id, f"searching [{country.upper()}]: {query}")

        for page_num in range(max_pages):
            if max_apps and self.jobs_applied >= max_apps:
                break

            # Search
            listings = await search_page.search(query, page_num)
            if not listings:
                logger.info(f"No more results on page {page_num + 1}")
                break

            self.jobs_found += len(listings)
            if agent_id:
                REGISTRY.inc("found", len(listings))

            # Process each listing
            for listing in listings:
                if max_apps and self.jobs_applied >= max_apps:
                    break

                await self._process_listing(
                    page, listing, job_page, mode, query, agent_id=agent_id
                )

            # Check for next page
            if not await search_page.has_next_page():
                break

            # Navigate back to search results for next page
            await between_pages()

        # End session
        await self.db.end_session(
            self.session_id,
            self.jobs_found,
            self.jobs_applied,
            self.jobs_skipped,
        )

    async def _process_listing(
        self,
        page,
        listing: JobListing,
        job_page: JobPage,
        mode: str,
        search_query: str,
        agent_id: str = "",
    ) -> None:
        """Process a single job listing: check → open → review → apply."""
        if agent_id and REGISTRY.is_stopped(agent_id):
            return

        # Skip if already applied
        if await self.db.is_already_applied(listing.indeed_job_id):
            logger.info(f"⏭ Already applied: {listing.title} @ {listing.company}")
            self.jobs_skipped += 1
            if agent_id:
                REGISTRY.inc("skipped")
            return

        # Skip if previously skipped in an earlier run
        if await self.db.is_already_skipped(listing.indeed_job_id):
            logger.info(f"Previously skipped: {listing.title} @ {listing.company}")
            self.jobs_skipped += 1
            if agent_id:
                REGISTRY.inc("skipped")
            return

        # Skip if not Easy Apply
        if not listing.is_easy_apply:
            logger.info(f"⏭ Not Easy Apply: {listing.title}")
            self.jobs_skipped += 1
            if agent_id:
                REGISTRY.inc("skipped")
            return

        # Record the job in DB
        job = Job(
            indeed_job_id=listing.indeed_job_id,
            title=listing.title,
            company=listing.company,
            location=listing.location,
            salary=listing.salary,
            url=listing.url,
            status="found",
        )
        await self.db.add_job(job)

        # Open job page
        if agent_id:
            _snap_agents = {a["agent_id"]: a for a in REGISTRY.snapshot().get("agents", [])}
            _cur_query = _snap_agents.get(agent_id, {}).get("query", "")
            REGISTRY.set_state(agent_id, "applying", query=_cur_query, current_job=listing.title)
        if not await job_page.open(listing.url, agent_id=agent_id):
            logger.warning(f"Failed to open: {listing.title}")
            self.jobs_skipped += 1
            if agent_id:
                REGISTRY.inc("skipped")
            return

        if agent_id:
            description = await job_page.get_description()
            job_info = {
                "title": listing.title,
                "company": listing.company,
                "location": listing.location,
                "salary": listing.salary or await job_page.get_salary(),
                "job_type": await job_page.get_job_type(),
                "description": description,
                "description_preview": description[:500] + "..." if len(description) > 500 else description,
                "url": listing.url,
            }
            REGISTRY.set_job_info(agent_id, job_info)

        # Check skip rules
        skip_keywords = self.config.get("skip_keywords", [])
        skip_companies = self.config.get("skip_companies", [])
        skip_reason = await job_page.should_skip(skip_keywords, skip_companies)
        if skip_reason:
            logger.info(f"⏭ Skipping: {listing.title} — {skip_reason}")
            await self.db.update_job_status(listing.indeed_job_id, "skipped", skip_reason)
            self.jobs_skipped += 1
            if agent_id:
                REGISTRY.inc("skipped")
                REGISTRY.append_log(agent_id, f"skipped (rule): {listing.title}")
            return

        # Verify Easy Apply button exists on detail page
        if not await job_page.has_easy_apply():
            logger.info(f"⏭ No Easy Apply button: {listing.title}")
            await self.db.update_job_status(listing.indeed_job_id, "skipped", "No Easy Apply button")
            self.jobs_skipped += 1
            if agent_id:
                REGISTRY.inc("skipped")
            return

        # Semi-auto mode: ask for user approval
        if mode == "semi":
            await job_page.display_summary()
            cc_print("\n[bold]Apply to this job?[/bold]")
            if agent_id:
                answer = await self._prompt_control_center(agent_id)
            else:
                answer = Prompt.ask(
                    "[cyan]y[/cyan]=apply, [yellow]s[/yellow]=skip, [red]q[/red]=quit",
                    choices=["y", "s", "q"],
                )
            if answer == "q":
                raise KeyboardInterrupt()
            if answer == "s":
                await self.db.update_job_status(listing.indeed_job_id, "skipped", "User skipped")
                self.jobs_skipped += 1
                if agent_id:
                    REGISTRY.inc("skipped")
                    REGISTRY.append_log(agent_id, f"user skipped: {listing.title}")
                return

        # Apply!
        console.print(
            f"[bold green]▶ Applying:[/bold green] {listing.title} @ {listing.company}"
        )

        try:
            # Click apply
            if not await job_page.click_apply():
                await self.db.update_job_status(listing.indeed_job_id, "failed", "Could not click Apply")
                self.jobs_failed += 1
                return

            # Complete the form
            resume_path = self._select_resume(listing.title)
            application_config = {
                **self.config,
                "resume": {
                    **self.config.get("resume", {}),
                    "default_path": resume_path,
                },
            }
            job_context = {
                "salary": listing.salary or await job_page.get_salary(),
                "description": await job_page.get_description(),
                "agent_id": agent_id,
                "mode": mode,
            }
            apply_form = ApplyForm(
                page,
                application_config,
                self.db,
                self.question_matcher,
                job_context,
            )
            success = await apply_form.complete_application(listing.indeed_job_id, resume_path)

            if success:
                await self.db.update_job_status(listing.indeed_job_id, "applied")
                self.jobs_applied += 1
                if agent_id:
                    REGISTRY.inc("applied")
                    REGISTRY.append_log(agent_id, f"✅ applied: {listing.title} @ {listing.company}")
                console.print(f"[bold green]✅ Applied! ({self.jobs_applied} total)[/bold green]")

                # Screenshot on success
                if self.config.get("bot", {}).get("screenshot_on_apply", True):
                    await capture_application_screenshot(
                        page,
                        search_query,
                        listing.title,
                        listing.company,
                    )
            else:
                if agent_id and REGISTRY.is_skip_requested(agent_id):
                    await self.db.update_job_status(listing.indeed_job_id, "skipped", "Skipped by user")
                    REGISTRY.clear_skip_request(agent_id)
                    self.jobs_skipped += 1
                    if agent_id:
                        REGISTRY.inc("skipped")
                        REGISTRY.append_log(agent_id, f"skipped: {listing.title}")
                    return
                await self.db.update_job_status(listing.indeed_job_id, "failed", "Form submission failed")
                self.jobs_failed += 1
                if agent_id:
                    REGISTRY.inc("failed")
                    REGISTRY.append_log(agent_id, f"❌ failed: {listing.title}")
                console.print("[red]❌ Application failed[/red]")

                if self.config.get("bot", {}).get("screenshot_on_error", True):
                    await capture_on_error(
                        page, Exception("Form submission failed"), listing.title
                    )

        except Exception as e:
            logger.error(f"Error applying to {listing.title}: {e}")
            await self.db.update_job_status(listing.indeed_job_id, "failed", str(e))
            self.jobs_failed += 1
            if agent_id:
                REGISTRY.inc("failed")
                REGISTRY.append_log(agent_id, f"❌ error: {listing.title} — {e}")

            if self.config.get("bot", {}).get("screenshot_on_error", True):
                await capture_on_error(page, e, listing.title)

    async def _prompt_control_center(self, agent_id: str) -> str:
        """Wait for an apply/skip/quit response via the control center."""
        REGISTRY.set_prompt(
            agent_id,
            "Apply to this job? (y=apply, s=skip, q=quit)",
            options=["y", "s", "q"],
        )
        REGISTRY.append_log(agent_id, "waiting for apply decision")

        while True:
            if REGISTRY.is_stopped(agent_id):
                REGISTRY.clear_prompt(agent_id)
                return "q"
            if REGISTRY.is_skip_requested(agent_id):
                REGISTRY.clear_prompt(agent_id)
                REGISTRY.clear_skip_request(agent_id)
                return "s"

            answer = REGISTRY.consume_answer(agent_id)
            if answer is not None:
                answer = answer.strip().lower()
                if answer in {"__skip_job__", "skip"}:
                    REGISTRY.clear_prompt(agent_id)
                    REGISTRY.clear_skip_request(agent_id)
                    return "s"
                if answer in {"y", "s", "q"}:
                    REGISTRY.clear_prompt(agent_id)
                    if answer == "s":
                        REGISTRY.clear_skip_request(agent_id)
                    return answer
                REGISTRY.append_log(agent_id, f"invalid answer: {answer!r}")

            await asyncio.sleep(0.5)

    # ------------------------------------------------------------------
    # Utility Commands
    # ------------------------------------------------------------------

    async def login_only(self) -> None:
        """Login and save session, then exit."""
        try:
            await self.initialize()
            page = await self.browser_manager.start()
            login_page = LoginPage(page)

            email = self.config.get("personal", {}).get("email", "")
            success = await login_page.login(email)

            if success:
                await self.browser_manager.save_session()
                console.print("[bold green]✅ Session saved! You can now run the bot.[/bold green]")
            else:
                console.print("[bold red]❌ Login failed.[/bold red]")
        finally:
            await self.shutdown()

    async def search_only(self, query: str | None = None) -> None:
        """Search and display results without applying."""
        try:
            await self.initialize()
            page = await self.browser_manager.start()
            login_page = LoginPage(page)

            if not await login_page.is_logged_in():
                email = self.config.get("personal", {}).get("email", "")
                await login_page.login(email)
                await self.browser_manager.save_session()

            queries = [query] if query else self.config.get("search", {}).get("queries", [])
            search_page = SearchPage(page, self.config)

            for q in queries:
                listings = await search_page.search(q)
                total = await search_page.get_total_results()
                console.print(f"\n[bold]Results for '{q}': {total}[/bold]\n")

                table = Table(title=f"Jobs: {q}", show_lines=True)
                table.add_column("#", style="dim", width=4)
                table.add_column("Title", style="cyan", max_width=40)
                table.add_column("Company", style="green", max_width=25)
                table.add_column("Location", max_width=20)
                table.add_column("Salary", style="yellow", max_width=20)
                table.add_column("Easy Apply", justify="center")

                for i, listing in enumerate(listings, 1):
                    ea = "[green]✓[/green]" if listing.is_easy_apply else "[red]✗[/red]"
                    table.add_row(
                        str(i),
                        listing.title,
                        listing.company,
                        listing.location,
                        listing.salary or "-",
                        ea,
                    )

                console.print(table)

        finally:
            await self.shutdown()

    async def show_status(self) -> None:
        """Display statistics from the database."""
        try:
            await self.initialize()
            stats = await self.db.get_stats()
            sessions = await self.db.get_sessions(limit=5)

            console.print(
                Panel(
                    f"[bold]Total Jobs Found:[/bold] {stats['total']}\n"
                    f"[green]Applied:[/green] {stats['applied']}\n"
                    f"[yellow]Skipped:[/yellow] {stats['skipped']}\n"
                    f"[red]Failed:[/red] {stats['failed']}\n"
                    f"[cyan]Success Rate:[/cyan] {stats['success_rate']}%",
                    title="📊 Bot Statistics",
                    border_style="cyan",
                )
            )

            if sessions:
                table = Table(title="Recent Sessions")
                table.add_column("Date", style="dim")
                table.add_column("Query", style="cyan")
                table.add_column("Location")
                table.add_column("Found", justify="right")
                table.add_column("Applied", justify="right", style="green")
                table.add_column("Skipped", justify="right", style="yellow")

                for s in sessions:
                    table.add_row(
                        s.started_at or "",
                        s.query,
                        s.location,
                        str(s.jobs_found),
                        str(s.jobs_applied),
                        str(s.jobs_skipped),
                    )
                console.print(table)

        finally:
            await self.shutdown()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _select_resume(self, job_title: str) -> str:
        """Select the best resume based on job title keywords.

        Args:
            job_title: Title of the job to match against resume profiles

        Returns:
            Path to the selected resume file
        """
        profiles = self.config.get("resume", {}).get("profiles", [])
        title_lower = job_title.lower()

        for profile in profiles:
            keywords = profile.get("keywords", [])
            if not keywords:
                continue  # Skip the "general" catch-all
            if any(kw.lower() in title_lower for kw in keywords):
                logger.info(f"📄 Selected resume profile: {profile['name']}")
                return profile["path"]

        # Fallback to default
        default = self.config.get("resume", {}).get("default_path", "config/resumes/Fahad Ali - Resume.pdf")
        logger.info(f"📄 Using default resume: {default}")
        return default

    def _print_summary(self) -> None:
        """Print session summary."""
        console.print("\n")
        console.print(
            Panel(
                f"[bold]Session Complete[/bold]\n\n"
                f"  Jobs found:   [bold]{self.jobs_found}[/bold]\n"
                f"  [green]Applied:      {self.jobs_applied}[/green]\n"
                f"  [yellow]Skipped:      {self.jobs_skipped}[/yellow]\n"
                f"  [red]Failed:       {self.jobs_failed}[/red]",
                title="📋 Summary",
                border_style="green" if self.jobs_applied > 0 else "yellow",
            )
        )
