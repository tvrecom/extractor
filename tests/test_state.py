import pytest

from tmdb_extractor.state import Database, TitleQueue


@pytest.fixture
def queue(tmp_path):
    db = Database(tmp_path / "s.sqlite3")
    yield TitleQueue(db), db
    db.close()


def test_add_pending_dedupes_and_counts_new(queue):
    q, _ = queue
    assert q.add_pending([1, 2, 3], "movie") == 3
    assert q.add_pending([2, 3, 4], "movie") == 1
    assert q.add_pending([1], "tv") == 1  # same id, different type is distinct


def test_claim_is_non_destructive(queue):
    q, _ = queue
    q.add_pending(range(1, 6), "movie")
    assert len(q.claim(3, 3, 0)) == 3
    assert len(q.claim(10, 3, 0)) == 5  # nothing was consumed by the first claim


def test_retry_quota_reserved_then_spare_capacity_used(queue):
    q, _ = queue
    q.add_pending(range(1, 11), "movie")
    for i in (1, 2, 3):
        q.mark_failed(i, "movie", "detail_fetch", "boom")
    items = q.claim(limit=4, max_retries=3, retry_quota=1)
    assert sum(1 for i in items if i.attempts > 0) == 1 and len(items) == 4
    # with fewer pending than the limit, spare slots are filled with retries
    q2_items = q.claim(limit=20, max_retries=3, retry_quota=0)
    assert len(q2_items) == 10


def test_exhausted_failures_not_claimed(queue):
    q, _ = queue
    q.add_pending([1], "movie")
    for _ in range(3):
        q.mark_failed(1, "movie", "x", "e")
    assert q.claim(5, max_retries=3, retry_quota=5) == []
    assert q.count_exhausted(3) == 1


def test_transaction_rolls_back_atomically(queue):
    q, db = queue
    with pytest.raises(RuntimeError):
        with db.transaction():
            q.add_pending([1, 2], "movie")
            raise RuntimeError("crash")
    assert q.counts()["pending"] == 0
