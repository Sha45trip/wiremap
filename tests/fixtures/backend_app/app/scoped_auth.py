from fastapi import APIRouter, Depends

from .auth import get_current_user
from .deps import CurrentUser  # auth type alias defined in another file

# router-scope auth: every route here is guarded (6.3) -> no missing_auth
secure = APIRouter(prefix="/secure", dependencies=[Depends(get_current_user)])


@secure.post("/thing")
def make_thing(payload: dict):
    # near-miss: mutating, no per-handler auth, but router guards it
    return {}


@secure.delete("/thing/{tid}")
def drop_thing(tid: int):
    return {}


# unguarded router: routes here still need their own auth
open_router = APIRouter(prefix="/open")


@open_router.post("/thing")
def open_make(payload: dict):
    # planted: mutating, unguarded router, no auth -> missing_auth
    return {}


@open_router.post("/decorated", dependencies=[Depends(get_current_user)])
def decorated(payload: dict):
    # near-miss: per-route decorator dependencies= guards it -> no flag
    return {}


@open_router.post("/annotated")
def annotated_dep(payload: dict, user: CurrentUser):
    # near-miss: annotated-dependency type alias guards it -> no flag
    return {}
