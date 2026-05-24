from __future__ import annotations

from starlist_bangumi.clients import LlmClient, OpenListClient, TmdbClient
from starlist_bangumi.config import PROJECT_ROOT, AppConfig, ConfigManager
from starlist_bangumi.run_index import RunIndex
from starlist_bangumi.services.executor import Executor
from starlist_bangumi.services.plan_builder import LlmPlanBuilder
from starlist_bangumi.services.scanner import SourceScanner
from starlist_bangumi.services.web_tasks import WebTaskManager


class AppRuntime:
    """Holds replaceable runtime components built from the current config."""

    def __init__(self, config_manager: ConfigManager) -> None:
        self.config_manager = config_manager
        self.config: AppConfig = config_manager.load()
        self.run_index = RunIndex(PROJECT_ROOT / "data" / "runs")
        self.openlist = OpenListClient(self.config.openlist)
        self.llm = LlmClient(self.config.llm)
        self.tmdb = TmdbClient(self.config.tmdb)
        self.scanner = SourceScanner(self.openlist, self.config)
        self.plan_builder = LlmPlanBuilder(self.scanner, self.llm, self.tmdb, self.config)
        self.executor = Executor(self.openlist)
        self.web_tasks = WebTaskManager(self)

    def rebuild(self) -> None:
        self.config = self.config_manager.load()
        self.openlist = OpenListClient(self.config.openlist)
        self.llm = LlmClient(self.config.llm)
        self.tmdb = TmdbClient(self.config.tmdb)
        self.scanner = SourceScanner(self.openlist, self.config)
        self.plan_builder = LlmPlanBuilder(self.scanner, self.llm, self.tmdb, self.config)
        self.executor = Executor(self.openlist)
        self.web_tasks._runtime = self
