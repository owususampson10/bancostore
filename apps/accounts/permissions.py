def is_distributor(user) -> bool:
    return user.groups.filter(name="distributor").exists()
