import asyncio
import logging

from app.core.config import settings
from app.db.models import Repository
from app.db.session import SessionLocal
from app.schemas.repos import RepoSyncRequest
from app.services.repos_sync import sync_repository_by_id

logger = logging.getLogger(__name__)


async def periodic_repo_sync_loop() -> None:
    # 定时兜底入口：Webhook 丢失或暂时不可用时，仍可周期性把远端状态拉回本地。
    await asyncio.sleep(5)
    while settings.auto_sync_enabled:
        db = SessionLocal()
        try:
            repos = (
                db.query(Repository)
                .filter(
                    Repository.provider.in_(["github", "github_compatible"]),
                    # 软删除仓库不再同步，等待保留期清理。
                    Repository.deleted_at.is_(None),
                )
                .all()
            )
            repo_ids = [repo.id for repo in repos]
        finally:
            db.close()

        # 并行同步，但限制同时进行的仓库数，避免一次打爆 GitHub 限流与数据库连接。
        semaphore = asyncio.Semaphore(max(1, settings.auto_sync_concurrency))

        async def run_one(repo_id) -> None:
            async with semaphore:
                try:
                    await sync_repository_by_id(repo_id, RepoSyncRequest(limit=settings.auto_sync_limit), trigger="scheduler")
                except Exception as exc:  # noqa: BLE001 - 单仓库失败不阻断其他仓库
                    logger.warning("Periodic repo sync failed for %s: %s", repo_id, exc)

        if repo_ids:
            await asyncio.gather(*(run_one(repo_id) for repo_id in repo_ids))

        try:
            from app.services.repo_lifecycle import purge_expired_soft_deleted_repositories

            # 清理要删仓库目录和向量数据，放线程池避免阻塞事件循环。
            purged = await asyncio.to_thread(purge_expired_soft_deleted_repositories)
            if purged:
                logger.info("调度器清理了 %s 个保留期已过的软删除仓库", purged)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Purge expired soft-deleted repositories failed: %s", exc)

        await asyncio.sleep(max(settings.auto_sync_interval_seconds, 30))
