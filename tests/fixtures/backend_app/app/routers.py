from fastapi import APIRouter

router = APIRouter(prefix="/api/v2")


@router.get("/health")
def health():
    return {"ok": True}
