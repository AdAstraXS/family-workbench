from datetime import timedelta

# Keep the DSM schedule and the user-facing cadence in sync.
COLLECTION_MINUTES = 30
STALE_AFTER = timedelta(minutes=COLLECTION_MINUTES * 2 + 5)
