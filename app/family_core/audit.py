def stamp_actor(instance, user):
    if not getattr(user, "is_authenticated", False):
        return instance
    if hasattr(instance, "created_by_id") and not instance.created_by_id:
        instance.created_by = user
    if hasattr(instance, "updated_by_id"):
        instance.updated_by = user
    return instance
