"""仅恢复旧懒注册租户的登录密码；在RAGFlow容器中运行，不重建租户或知识库。"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path


def recover(*, user_service, user_model, tenant_id, password, backup):
    email = hashlib.sha1(tenant_id.encode()).hexdigest()[:16] + "@kb.internal"
    users = user_service.query_user_by_email(email)
    if len(users) != 1 or users[0].is_superuser:
        raise ValueError("expected one non-platform tenant owner")
    user = users[0]
    before = {"id": user.id, "password": user.password, "access_token": user.access_token}
    if password is None:
        return {"email": email, "user_id": user.id, "action": "checked"}
    if len(password) < 24 or len(password) > 128 or any(c.isspace() for c in password):
        raise ValueError("a generated credential is required")
    # 密文中的旧散列和会话标识仅保存在容器受控备份，不进入输出或版本控制。
    with backup.open("x", encoding="utf-8") as output:
        os.chmod(backup, 0o600)
        json.dump(before, output)
    user_service.update_user_password(user.id, password)
    current = user_model.get_by_id(user.id)
    if current.access_token != before["access_token"]:
        raise ValueError("unexpected session change; retain backup for recovery")
    return {"email": email, "user_id": user.id, "action": "password_recovered"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    if args.apply and not args.backup:
        parser.error("--apply requires a new private backup path")
    os.umask(0o077)
    import logging

    logging.disable(logging.CRITICAL)
    from api.db.db_models import User
    from api.db.services.user_service import UserService

    password = sys.stdin.read().strip() if args.apply else None
    print(
        json.dumps(
            recover(
                user_service=UserService,
                user_model=User,
                tenant_id=args.tenant,
                password=password,
                backup=args.backup,
            )
        )
    )


if __name__ == "__main__":
    main()
