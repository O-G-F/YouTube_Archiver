"""Environment diagnostics endpoint (Phase 1.5)."""

from __future__ import annotations

from fastapi import APIRouter

from app.schemas import DoctorOut
from app.services.doctor import run_diagnostics

router = APIRouter(tags=["doctor"])


@router.get("/api/doctor", response_model=DoctorOut)
def doctor() -> DoctorOut:
    return DoctorOut(**run_diagnostics())
