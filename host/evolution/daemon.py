"""
演化 Daemon（Evolution Daemon）

生物演化不是即時的 — 它在一個族群的多個世代中緩慢累積。
EvoClaw 的演化 daemon 模擬這個過程：
  每 24 小時執行一次「演化週期」，
  根據過去一段時間的執行數據，調整各群組的基因組。

演化週期流程：
  1. 掃描所有有活躍記錄的群組
  2. 計算各群組的適應度分數
  3. 根據適應度和回應時間，調整群組基因組
  4. 記錄演化結果（世代++）

設計原則：
  - 演化週期是獨立的 asyncio Task，崩潰不影響主訊息流程
  - 每次只做小幅調整，避免劇烈改變破壞已建立的用戶期望
  - 第一次演化需要足夠的樣本數（MIN_SAMPLES），避免噪音驅動的演化
"""

import asyncio
import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

# 演化週期間隔（秒）：預設 1 小時（原為 24 小時，對測試不友好）
EVOLUTION_INTERVAL_SECS = 3600

# 觸發演化所需的最少執行樣本數
# 樣本太少時跳過，避免少數異常值導致錯誤的演化方向
# 降低至 3（原為 10），讓群組更容易達到觸發門檻
MIN_SAMPLES = 3

# 計算適應度時的回顧時間視窗（天）
FITNESS_WINDOW_DAYS = 7

# 三層記憶系統：Micro Sync 間隔（3 小時）
_MICRO_SYNC_INTERVAL_SECS = 3 * 3600
# 三層記憶系統：Weekly Compound 間隔（7 天）
_WEEKLY_COMPOUND_INTERVAL_SECS = 7 * 86400

# 上次執行時間追蹤 (Fix #205: loaded from DB at first check to survive restarts)
_last_micro_sync: float = 0.0
_last_weekly_compound: float = 0.0
_sync_timestamps_loaded: bool = False


async def evolution_loop(stop_event: asyncio.Event) -> None:
    """
    演化主迴圈：每 EVOLUTION_INTERVAL_SECS 秒執行一次演化週期。

    作為獨立的 asyncio Task 啟動（在 main.py 的 main() 中）。
    使用 stop_event.wait() 讓出控制權給其他 Task，不阻塞訊息處理，
    並可在收到 shutdown 訊號時立即中斷等待。

    第一次等待後才執行（系統啟動時沒有足夠數據，等 24 小時再說）。
    """
    log.info("Evolution daemon started (first cycle in 1h)")
    while True:
        # 先等待，再執行（讓系統先跑一段時間累積數據）
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=EVOLUTION_INTERVAL_SECS)
            # stop_event was set — shutdown requested
            log.info("Evolution daemon shutting down")
            break
        except asyncio.TimeoutError:
            pass  # 24h elapsed, run evolution cycle
        try:
            await _run_cycle()
        except asyncio.CancelledError:
            # 系統關閉時，優雅退出演化迴圈
            log.info("Evolution daemon shutting down")
            break
        except Exception as e:
            # 演化週期失敗不應中斷 daemon，記錄錯誤後繼續下一個週期
            log.error(f"Evolution cycle failed: {e}", exc_info=True)

        # Periodic DB maintenance: prune old rows from all log tables.
        # Running here means maintenance happens every 24h without an additional loop,
        # ensuring long-running processes don't accumulate rows indefinitely.
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_sync_prune_logs),
                timeout=300.0,
            )
        except asyncio.TimeoutError:
            log.warning("evolution: prune_logs timed out after 300s — skipping this cycle")
        except Exception as e:
            log.warning("Periodic log pruning failed (non-fatal): %s", e)

        # 三層記憶系統：定期執行 Micro Sync（每 3 小時）
        global _last_micro_sync, _last_weekly_compound, _sync_timestamps_loaded
        now = time.time()

        # Fix #205: load persisted timestamps on first cycle to avoid running
        # micro_sync/weekly_compound immediately after every process restart.
        if not _sync_timestamps_loaded:
            _sync_timestamps_loaded = True
            try:
                from host import db as _ts_db
                _sync_rows = _ts_db.get_db().execute(
                    "SELECT MAX(last_micro_sync), MAX(last_weekly_compound) FROM group_memory_sync"
                ).fetchone()
                if _sync_rows and _sync_rows[0]:
                    _last_micro_sync = float(_sync_rows[0])
                if _sync_rows and _sync_rows[1]:
                    _last_weekly_compound = float(_sync_rows[1])
                log.info("Loaded sync timestamps: micro=%.0f weekly=%.0f", _last_micro_sync, _last_weekly_compound)
            except Exception as _ts_exc:
                log.warning("Failed to load sync timestamps (non-fatal): %s", _ts_exc)
        if now - _last_micro_sync >= _MICRO_SYNC_INTERVAL_SECS:
            try:
                await _run_memory_micro_sync()
                _last_micro_sync = now
            except Exception as e:
                log.warning("Memory micro_sync failed (non-fatal): %s", e)

        # 三層記憶系統：定期執行 Weekly Compound（每 7 天）
        if now - _last_weekly_compound >= _WEEKLY_COMPOUND_INTERVAL_SECS:
            try:
                await _run_memory_weekly_compound()
                _last_weekly_compound = now
            except Exception as e:
                log.warning("Memory weekly_compound failed (non-fatal): %s", e)


async def _run_cycle() -> None:
    """
    執行一次完整的演化週期（同步邏輯包在 run_in_executor 中）。

    DB 查詢都是同步的（sqlite3），用 executor 避免阻塞 event loop。
    """
    log.info("Evolution cycle starting")
    await asyncio.to_thread(_sync_evolve)
    log.info("Evolution cycle complete")


def _sync_prune_logs() -> None:
    """Run DB log pruning and WAL checkpoint synchronously (called via asyncio.to_thread).

    After pruning old rows the WAL file may still be large because SQLite only
    reclaims WAL pages at checkpoint time.  Running PRAGMA wal_checkpoint(TRUNCATE)
    here ensures the WAL is truncated every 24h, preventing unbounded WAL growth
    on high-traffic deployments (Issue #62).
    """
    from host import db
    db.prune_old_logs(days=30)
    log.info("Periodic log pruning completed")
    try:
        with db._db_lock:
            conn = db.get_db()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        log.info("WAL checkpoint (TRUNCATE) completed")
    except Exception as exc:
        log.warning("WAL checkpoint failed (non-fatal): %s", exc)


def _sync_evolve() -> None:
    """
    演化週期的同步核心邏輯。

    在 executor 中執行，避免 sqlite3 阻塞 asyncio event loop。
    """
    from host import db
    from host.evolution.fitness import compute_fitness
    from host.evolution.genome import evolve_genome_from_fitness

    # 取得所有在過去 FITNESS_WINDOW_DAYS 天內有執行記錄的群組
    try:
        active_jids = db.get_active_evolution_jids(days=FITNESS_WINDOW_DAYS)
    except Exception as e:
        log.error(f"Failed to get active JIDs: {e}")
        return

    log.info(f"Evolution cycle: evaluating {len(active_jids)} group(s)")

    # 記錄週期開始
    try:
        db.log_evolution_event(
            jid="__system__",
            event_type="cycle_start",
            notes=f"Evaluating {len(active_jids)} group(s)",
        )
    except Exception:
        pass

    evolved_count = 0
    skipped_count = 0

    for jid in active_jids:
        try:
            # 取得最近的執行記錄
            runs = db.get_evolution_runs(jid, FITNESS_WINDOW_DAYS)
            if len(runs) < MIN_SAMPLES:
                # 樣本不足，跳過此群組（避免少數噪音驅動演化）
                log.debug(f"Skip {jid}: only {len(runs)} samples (need {MIN_SAMPLES})")
                skipped_count += 1
                try:
                    db.log_evolution_event(
                        jid=jid,
                        event_type="skipped_low_samples",
                        notes=f"Only {len(runs)} samples (need {MIN_SAMPLES})",
                    )
                except Exception:
                    pass
                continue

            # 計算綜合適應度分數
            fitness = compute_fitness(jid, FITNESS_WINDOW_DAYS)

            # 計算平均回應時間（用於回答風格調整）
            valid_times = [r["response_ms"] for r in runs if r.get("response_ms")]
            avg_ms = sum(valid_times) / len(valid_times) if valid_times else 0

            # 執行基因組演化（根據適應度和速度調整行為參數）
            # (此函式內部會呼叫 db.log_evolution_event)
            evolve_genome_from_fitness(jid, fitness, avg_ms)
            evolved_count += 1

            log.info(f"Evolved {jid}: fitness={fitness:.3f}, avg_ms={avg_ms:.0f}")

        except Exception as e:
            log.warning(f"Failed to evolve {jid}: {e}")
            continue

    log.info(f"Evolution cycle done: {evolved_count}/{len(active_jids)} groups evolved")

    # 記錄週期結束
    try:
        db.log_evolution_event(
            jid="__system__",
            event_type="cycle_end",
            notes=f"evolved={evolved_count}, skipped={skipped_count}, total={len(active_jids)}",
        )
    except Exception:
        pass


async def _run_memory_micro_sync() -> None:
    """
    三層記憶系統：對所有已登記群組執行 Micro Sync（每 3 小時）。
    掃描近期暖記憶日誌，提取關鍵決策並更新熱記憶。
    """
    from host import db as _db
    from host.memory.warm import run_micro_sync
    try:
        groups = _db.get_all_registered_groups()
        for group in groups:
            jid = group.get("jid", "")
            if jid:
                await run_micro_sync(jid)
        log.info("memory: micro_sync completed for %d group(s)", len(groups))
    except Exception as exc:
        log.warning("memory: micro_sync cycle failed: %s", exc)


async def _run_memory_weekly_compound() -> None:
    """
    三層記憶系統：對所有已登記群組執行 Weekly Compound（每 7 天）。
    剪除 30 天以上的低價值暖記憶，並蒸餾 pattern 至熱記憶。
    """
    from host import db as _db
    from host.memory.compound import run_weekly_compound
    try:
        groups = _db.get_all_registered_groups()
        for group in groups:
            jid = group.get("jid", "")
            if jid:
                await run_weekly_compound(jid)
        log.info("memory: weekly_compound completed for %d group(s)", len(groups))
    except Exception as exc:
        log.warning("memory: weekly_compound cycle failed: %s", exc)
