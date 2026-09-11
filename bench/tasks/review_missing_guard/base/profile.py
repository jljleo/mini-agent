"""用户资料展示。"""


def display_name(user: dict | None) -> str:
    """展示名：匿名用户显示「访客」，否则取昵称，无昵称回退邮箱前缀。"""
    if user is None:
        return "访客"
    nickname = user.get("profile", {}).get("nickname", "").strip()
    if nickname:
        return nickname
    return user.get("email", "").split("@")[0]
