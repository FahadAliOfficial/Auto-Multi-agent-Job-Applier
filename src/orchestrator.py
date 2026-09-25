from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from src.control_center import REGISTRY
from src.pages.job_page import JobPage
from src.pages.search_page import SearchPage
from src.utils.logger import reset_current_agent, set_current_agent


@dataclass
class AgentConfig:
    agent_id: str
    queries: list[str]
    mode: str
    max_apps: int


class AgentOrchestrator:
    """Run multiple apply agents concurrently over query partitions."""

    def __init__(self, bot: Any):
        self.bot = bot
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()

    async def run(self, mode: str, max_apps: int, initial_page) -> None:
        cfg = self.bot.config.get("bot", {})
        agents_count = int(cfg.get("agents", 1))
        orch_cfg = cfg.get("orchestrator", {}) or {}
        if not orch_cfg.get("enabled", True):
            agents_count = 1
        agents_count = max(1, min(agents_count, int(orch_cfg.get("max_concurrent_agents", agents_count))))

        queries = self.bot.config.get("search", {}).get("queries", [])
        if not queries:
            return

        await self.bot.db.cleanup_stale_claims()
        pages = [initial_page]
        for _ in range(agents_count - 1):
            pages.append(await self.bot.browser_manager.context.new_page())

        partitions = [[] for _ in range(agents_count)]
        for idx, q in enumerate(queries):
            partitions[idx % agents_count].append(q)

        agent_cfgs = [
            AgentConfig(agent_id=f"agent-{i+1}", queries=partitions[i], mode=mode, max_apps=max_apps)
            for i in range(agents_count)
        ]
        for ac in agent_cfgs:
            REGISTRY.ensure_agent(ac.agent_id)

        tasks = [
            asyncio.create_task(self._run_agent(ac, pages[i]), name=ac.agent_id)
            for i, ac in enumerate(agent_cfgs)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for idx, result in enumerate(results):
            if isinstance(result, Exception):
                agent_id = agent_cfgs[idx].agent_id
                REGISTRY.append_log(agent_id, f"agent task crashed: {result}")

    async def _run_agent(self, cfg: AgentConfig, page) -> None:
        token = set_current_agent(cfg.agent_id)
        try:
            REGISTRY.set_state(cfg.agent_id, "idle")
            REGISTRY.append_log(cfg.agent_id, f"started with {len(cfg.queries)} query(s)")
            job_page = JobPage(page)
            if self.bot.browser_manager and self.bot.browser_manager.context:
                job_page.set_context(self.bot.browser_manager.context)
            max_pages = self.bot.config.get("search", {}).get("max_pages", 5)

            orch_cfg = self.bot.config.get("bot", {}).get("orchestrator", {}) or {}
            countries: list[str] = orch_cfg.get("countries", ["us"])

            for country in countries:
                country = country.lower()
                REGISTRY.append_log(cfg.agent_id, f"--- country: {country.upper()} ---")
                search_page = SearchPage(page, self.bot.config, country=country)

                for query in cfg.queries:
                    if REGISTRY.is_stopped(cfg.agent_id):
                        REGISTRY.set_state(cfg.agent_id, "stopped")
                        REGISTRY.append_log(cfg.agent_id, "stopped")
                        break
                    await self._service_agent_actions(cfg.agent_id, page)
                    if self._stop.is_set():
                        break
                    if cfg.max_apps and self.bot.jobs_applied >= cfg.max_apps:
                        break
                    if not await REGISTRY.wait_if_paused(cfg.agent_id):
                        break

                    REGISTRY.set_state(cfg.agent_id, "searching", query=query)
                    REGISTRY.append_log(cfg.agent_id, f"searching query: {query} [{country.upper()}]")
                    location = self.bot.config.get("search", {}).get("location", "")
                    session_id = await self.bot.db.start_session(query, location)
                    query_found = 0
                    query_applied = 0
                    query_skipped = 0
                    for page_num in range(max_pages):
                        if REGISTRY.is_stopped(cfg.agent_id):
                            REGISTRY.set_state(cfg.agent_id, "stopped")
                            REGISTRY.append_log(cfg.agent_id, "stopped")
                            break
                        await self._service_agent_actions(cfg.agent_id, page)
                        if cfg.max_apps and self.bot.jobs_applied >= cfg.max_apps:
                            break
                        listings = await search_page.search(query, page_num)
                        if not listings:
                            REGISTRY.append_log(cfg.agent_id, f"no results on page {page_num + 1}")
                            break
                        query_found += len(listings)
                        self.bot.jobs_found += len(listings)
                        REGISTRY.append_log(cfg.agent_id, f"page {page_num + 1}: found {len(listings)} listing(s)")
                        REGISTRY.inc("found", len(listings))

                        for listing in listings:
                            if REGISTRY.is_stopped(cfg.agent_id):
                                REGISTRY.set_state(cfg.agent_id, "stopped")
                                REGISTRY.append_log(cfg.agent_id, "stopped")
                                break
                            await self._service_agent_actions(cfg.agent_id, page)
                            if cfg.max_apps and self.bot.jobs_applied >= cfg.max_apps:
                                break
                            if not await REGISTRY.wait_if_paused(cfg.agent_id):
                                break
                            REGISTRY.set_state(cfg.agent_id, "applying", query=query, current_job=listing.title)
                            REGISTRY.append_log(cfg.agent_id, f"trying: {listing.title} @ {listing.company}")
                            claimed = await self.bot.db.claim_job(listing.indeed_job_id, cfg.agent_id)
                            if not claimed:
                                REGISTRY.append_log(cfg.agent_id, f"skip claimed {listing.title}")
                                continue
                            try:
                                await self.bot._process_listing(page, listing, job_page, cfg.mode, query, cfg.agent_id)
                                job = await self.bot.db.get_job(listing.indeed_job_id)
                                if job and job.status == "applied":
                                    await self.bot.db.set_job_claim_status(listing.indeed_job_id, "applied")
                                    REGISTRY.inc("applied", 1)
                                    REGISTRY.append_log(cfg.agent_id, f"applied: {listing.title}")
                                    query_applied += 1
                                elif job and job.status == "skipped":
                                    await self.bot.db.release_job_claim(listing.indeed_job_id, cfg.agent_id)
                                    REGISTRY.inc("skipped", 1)
                                    REGISTRY.append_log(cfg.agent_id, f"skipped: {listing.title}")
                                    query_skipped += 1
                                else:
                                    await self.bot.db.release_job_claim(listing.indeed_job_id, cfg.agent_id)
                                    REGISTRY.inc("failed", 1)
                                    REGISTRY.append_log(cfg.agent_id, f"failed: {listing.title}")
                                    if REGISTRY.consume_retry_flag(cfg.agent_id):
                                        REGISTRY.append_log(cfg.agent_id, f"retrying {listing.title}")
                                        re_claim = await self.bot.db.claim_job(listing.indeed_job_id, cfg.agent_id)
                                        if re_claim:
                                            await self.bot._process_listing(page, listing, job_page, cfg.mode, query, cfg.agent_id)
                            except Exception as exc:
                                await self.bot.db.release_job_claim(listing.indeed_job_id, cfg.agent_id)
                                REGISTRY.append_log(cfg.agent_id, f"error {exc}")
                                REGISTRY.inc("failed", 1)

                        if not await search_page.has_next_page():
                            break
                    await self.bot.db.end_session(
                        session_id,
                        query_found,
                        query_applied,
                        query_skipped,
                    )

                if REGISTRY.is_stopped(cfg.agent_id):
                    break
                if cfg.max_apps and self.bot.jobs_applied >= cfg.max_apps:
                    break

            if REGISTRY.is_stopped(cfg.agent_id):
                REGISTRY.set_state(cfg.agent_id, "stopped")
                REGISTRY.append_log(cfg.agent_id, "stopped")
            else:
                REGISTRY.set_state(cfg.agent_id, "done")
                REGISTRY.append_log(cfg.agent_id, "done")
            REGISTRY.set_captcha_wait_count()
        finally:
            reset_current_agent(token)

    async def _service_agent_actions(self, agent_id: str, page) -> None:
        if REGISTRY.is_stopped(agent_id):
            try:
                await page.close()
                REGISTRY.append_log(agent_id, "closed browser tab")
            except Exception:
                pass
            return
        if REGISTRY.consume_focus_flag(agent_id):
            try:
                await page.bring_to_front()
                REGISTRY.append_log(agent_id, "brought browser tab to front")
            except Exception as exc:
                REGISTRY.append_log(agent_id, f"focus failed: {exc}")
