from fastapi import (
    FastAPI,
    HTTPException,
    status,
    APIRouter,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from dataclasses import asdict
import asyncio
import secrets
import time

from app_state import app_state
from app_linux import LinuxCPUController, MetricsFetcher, DynamicScriptEngine

app = FastAPI(title="EdgeLab CPU Controller API")
cpu_controller = LinuxCPUController()
metrics_controller = MetricsFetcher()
scripting_controller = DynamicScriptEngine(
    app_state=app_state, cpu_controller=cpu_controller
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- PYDANTIC SCHEMAS ---


class GovernorInput(BaseModel):
    governor: str


class FrequencyInput(BaseModel):
    minFreq: Optional[float] = None
    maxFreq: Optional[float] = None


class GovernorParamsInput(BaseModel):
    thresholdUp: Optional[int] = None
    thresholdDown: Optional[int] = None
    samplingRate: Optional[int] = None
    samplingDownFactor: Optional[int] = None
    frequencyStep: Optional[int] = None
    rateLimit: Optional[int] = None
    powerBias: Optional[int] = None
    isIgnoreNice: Optional[bool] = None
    isIoBusy: Optional[bool] = None
    fixedFrequency: Optional[float] = None
    script: str
    isDynamicScripting: bool = True


class UpdateScriptPayload(BaseModel):
    script: str
    isDynamicScripting: bool = True


# 1. GET CURRENT CPU STATUS
@app.get("/api/cpu/status")
def get_current_hardware_status():
    try:
        governors_dict = cpu_controller.get_governors()
        governor = governors_dict.get("cpu0", "powersave")

        # Memperkuat deteksi jika driver cpufreq bermasalah
        if "Error" in governor or "Permission" in governor:
            governor = app_state.cpu.governor
        else:
            app_state.cpu.governor = governor

        hardware_data = cpu_controller.get_governor_state()

        # Sync to Global CPU State
        if "minFreq" in hardware_data:
            app_state.cpu.minFreq = hardware_data["minFreq"]
        if "maxFreq" in hardware_data:
            app_state.cpu.maxFreq = hardware_data["maxFreq"]

        # Sync to Active Governor Sub-State
        sub_state = getattr(app_state.cpu, governor, None)
        if sub_state and hardware_data:
            for key, val in hardware_data.items():
                if hasattr(sub_state, key):
                    setattr(sub_state, key, val)

        return {
            "status": "success",
            "governor": governor,
            "minFreq": app_state.cpu.minFreq,
            "maxFreq": app_state.cpu.maxFreq,
            "tunables": {
                k: v
                for k, v in hardware_data.items()
                if k not in ["minFreq", "maxFreq"]
            },
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal Server Error while fetching CPU status: {str(e)}",
        )


# 2. UPDATE GOVERNOR SELECTION
@app.post("/api/cpu/governor")
async def handle_governor_state(payload: GovernorInput):
    try:
        governor = payload.governor
        success = cpu_controller.apply_cpu_governor(governor)

        if not success:
            # Diubah ke 400 Bad Request jika nama governor salah / tidak didukung hardware
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to apply governor '{governor}'. Verify it is supported by your Linux system.",
            )

        app_state.cpu.governor = governor
        return get_current_hardware_status()

    except HTTPException as http_err:
        raise http_err
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error applying governor: {str(e)}",
        )


# 3. UPDATE GLOBAL FREQUENCIES
@app.post("/api/cpu/frequency")
def handle_cpu_frequency(payload: FrequencyInput):
    try:
        if payload.minFreq is None and payload.maxFreq is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Bad Request: Must provide at least minFreq or maxFreq.",
            )

        # Sanity check: Frekuensi tidak boleh bernilai minus
        if (payload.minFreq is not None and payload.minFreq < 0) or (
            payload.maxFreq is not None and payload.maxFreq < 0
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Validation Error: Frequency values cannot be negative.",
            )

        # Apply to hardware
        success = cpu_controller.apply_cpu_frequencies(payload.minFreq, payload.maxFreq)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to update frequency. Ensure minFreq is not greater than maxFreq and hardware limits are respected.",
            )

        # Update local app state
        if payload.minFreq is not None:
            app_state.cpu.minFreq = payload.minFreq
        if payload.maxFreq is not None:
            app_state.cpu.maxFreq = payload.maxFreq

        return {
            "status": "success",
            "minFreq": app_state.cpu.minFreq,
            "maxFreq": app_state.cpu.maxFreq,
        }
    except HTTPException as http_err:
        raise http_err
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error modifying CPU frequencies: {str(e)}",
        )


# 4. UPDATE GOVERNOR TUNABLES
@app.post("/api/cpu/governor/params")
async def handle_governor_params(payload: GovernorParamsInput):
    try:
        governor = app_state.cpu.governor
        sub_state = getattr(app_state.cpu, governor, None)

        if not sub_state:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Governor '{governor}' is active but does not have tunable parameters or isn't configurable.",
            )

        incoming_params = payload.model_dump(exclude_unset=True)
        if not incoming_params:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing parameters: Payload parameter is empty.",
            )

        # Validate layout parameters compatibility
        for key in incoming_params.keys():
            if not hasattr(sub_state, key):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid parameter '{key}' for the currently active governor '{governor}'.",
                )

        # VALIDASI SCRIPTING
        target_is_dynamic = incoming_params.get(
            "isDynamicScripting",
            getattr(app_state.cpu.userspace, "isDynamicScripting", False),
        )
        target_script = (
            incoming_params.get(
                "script", getattr(app_state.cpu.userspace, "script", "")
            )
            or ""
        )
        if governor == "userspace" and target_is_dynamic:
            if not target_script.strip():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Script tidak boleh kosong saat Dynamic Scripting aktif.",
                )
            try:
                compile(target_script, "<userspace_script>", "exec")
            except SyntaxError as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Syntax error, script: {e.msg} (Line {e.lineno})",
                )

        # Apply to hardware
        success = cpu_controller.apply_governor_params(governor, incoming_params)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Kernel Write Error: Failed to write internal parameters to kernel sysfs.",
            )

        # Update local state
        for key, val in incoming_params.items():
            setattr(sub_state, key, val)

        return get_current_hardware_status()

    except HTTPException as http_err:
        raise http_err
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error handling governor tunables: {str(e)}",
        )


from fastapi import HTTPException, status


@app.post("/api/cpu/userspace/start")
async def start_dynamic_scripting():
    current_governor = getattr(app_state.cpu, "governor", "")
    is_dynamic = getattr(app_state.cpu.userspace, "isDynamicScripting", False)
    script_content = getattr(app_state.cpu.userspace, "script", "") or ""

    if current_governor != "userspace" or not is_dynamic:
        metrics_controller.stop()
        scripting_controller.stop()
        return {
            "status": "stopped",
            "message": "Scripting stop!",
        }

    if not script_content.strip():
        metrics_controller.stop()
        scripting_controller.stop()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Script kosong",
        )

    try:
        compile(script_content, "<userspace_script>", "exec")
    except SyntaxError as e:
        metrics_controller.stop()
        scripting_controller.stop()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Syntax error, script: {e.msg} (Line {e.lineno})",
        )

    metrics_controller.start()
    scripting_controller.start()

    return {
        "status": "success",
        "message": "Dynamic scripting started successfully.",
    }


@app.post("/api/cpu/userspace/stop")
async def stop_dynamic_scripting():
    metrics_controller.stop()
    scripting_controller.stop()
    return {
        "status": "success",
        "message": "Dynamic scripting engine berhasil dihentikan.",
    }


# 6. DEBUG LOGS
@app.get("/log")
def get_full_app_state():
    try:
        return {"status": "success", "app_state": asdict(app_state)}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to dump application state state: {str(e)}",
        )


#  ==== SOCKET ====

router = APIRouter()


# Utilization Core
@router.websocket("/ws/utilization")
async def cpu_websocket(websocket: WebSocket):
    await websocket.accept(headers=[(b"access-control-allow-origin", b"*")])
    try:
        while True:
            # Panggil fungsi dari modul linux
            data = cpu_controller.get_cpu_utilization(max_cores=4)
            await websocket.send_json(data)
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        print("Client disconnected from core websocket")


@router.websocket("/ws/metrics")
async def cpu_status_websocket(websocket: WebSocket):
    await websocket.accept(headers=[(b"access-control-allow-origin", b"*")])
    try:
        while True:
            data = cpu_controller.get_cpu_status()
            await websocket.send_json(data)
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        print("Client disconnected from status websocket")


# ==== SESSION ====

ACTIVE_SESSION = {"token": None, "expires_at": 0}


class TokenPayload(BaseModel):
    token: str | None = None


@router.post("/api/session/check")
async def check_or_create_session(payload: TokenPayload):
    current_time = time.time()

    if payload.token and ACTIVE_SESSION["token"] == payload.token:
        ACTIVE_SESSION["expires_at"] = current_time + 10
        return {"status": "authorized", "token": payload.token}

    if (
        ACTIVE_SESSION["token"] is not None
        and ACTIVE_SESSION["expires_at"] > current_time
    ):
        raise HTTPException(
            status_code=403,
            detail="The device is being accessed in another tab or browser.",
        )

    new_token = secrets.token_hex(16)
    ACTIVE_SESSION["token"] = new_token
    ACTIVE_SESSION["expires_at"] = current_time + 10  # Berlaku 10 detik kedepan

    return {"status": "authorized", "token": new_token}


@router.post("/api/session/heartbeat")
async def session_heartbeat(payload: TokenPayload):
    current_time = time.time()

    if not payload.token or ACTIVE_SESSION["token"] != payload.token:
        raise HTTPException(status_code=403, detail="Session invalid.")

    ACTIVE_SESSION["expires_at"] = current_time + 10
    return {"status": "alive"}


app.include_router(router)
