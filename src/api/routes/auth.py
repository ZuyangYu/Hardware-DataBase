from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException

from src.core.auth import AuthService

from src.api.deps import bearer_token, current_user, get_auth_service
from src.api.schemas import LoginRequest, LoginResponse, OkResponse, UserInfo

router = APIRouter(tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, auth: AuthService = Depends(get_auth_service)) -> LoginResponse:
    session = auth.authenticate(body.username, body.password)
    if session is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    u = session.user
    return LoginResponse(
        token=session.token,
        user=UserInfo(
            username=u.username,
            role=u.role,
            department_id=u.department_id,
            department_name=u.department_name,
        ),
    )


@router.get("/whoami", response_model=UserInfo)
def whoami(user=Depends(current_user)) -> UserInfo:
    return UserInfo(
        username=user.username,
        role=user.role,
        department_id=user.department_id,
        department_name=user.department_name,
    )


@router.post("/logout", response_model=OkResponse)
def logout(
    authorization: str | None = Header(default=None),
    user=Depends(current_user),
    auth: AuthService = Depends(get_auth_service),
) -> OkResponse:
    # current_user already rejected empty/invalid tokens with 401; pass the
    # resolved user into revoke_session so it doesn't re-query get_user_by_token.
    auth.revoke_session(bearer_token(authorization), actor=user)
    return OkResponse(ok=True, message="logged out")
