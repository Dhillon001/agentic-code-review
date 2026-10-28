def has_admin(users) -> bool:
    return any(user.role == "admin" for user in users)
