
from fastapi import Depends, HTTPException, status

from app.auth import get_current_user

ALL_PERMISSIONS = {
    "properties.view",
    "properties.review",
    "users.view",
    "users.manage",
    "reports.view",
    "activity.view",
    "revenue.view",
}


def require_super_admin(user: dict = Depends(get_current_user)):
    if user.get("role") != "super_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super Admin access required",
        )
    return user


def require_permission(permission: str):
    async def checker(user: dict = Depends(get_current_user)):
        # Super Admin bypasses individual permission checks.
        if user.get("role") == "super_admin":
            return user

        if user.get("role") != "admin":
            raise HTTPException(
                status_code=403,
                detail="Admin access required",
            )

        if permission not in user.get("permissions", []):
            raise HTTPException(
                status_code=403,
                detail=f"Missing permission: {permission}",
            )

        return user

    return checker
