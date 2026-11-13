from roro_resource_manager import BoundedEventQueue, ResourceManager


def test_resource_manager_removes_expired_cache_entries_only():
    manager = ResourceManager(max_cache_items=10)
    manager.cache_set("expired", {"value": 1}, ttl_s=0)
    manager.cache_set("fresh", {"value": 2}, ttl_s=60)

    result = manager.cleanup()

    assert result["removed"] == 1
    assert manager.cache_get("expired") is None
    assert manager.cache_get("fresh") == {"value": 2}


def test_bounded_event_queue_preserves_critical_events():
    events = BoundedEventQueue(maxsize=2, critical_reserve=1)
    assert events.put("routine-1")
    assert events.put("routine-2")

    assert events.put("execution-uncertain", critical=True)
    assert events.qsize() == 2
    assert events.get() == "routine-2"
    assert events.get() == "execution-uncertain"


def test_bounded_event_queue_rejects_critical_only_overflow_without_dropping_critical():
    events = BoundedEventQueue(maxsize=1, critical_reserve=1)
    assert events.put("execution-uncertain", critical=True)

    assert not events.put("another-critical", critical=True)
    assert events.get() == "execution-uncertain"
    assert events.rejected_critical == 1
