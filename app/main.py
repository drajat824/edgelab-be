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

from .state.app_state import app_state
from .linux.app_linux import LinuxCPUController

app = FastAPI(title="EdgeLab CPU Controller API")
cpu_controller = LinuxCPUController()

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
            "cores": app_state.model.cores,
            "numThread": app_state.model.num_threads,
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
def handle_governor_state(payload: GovernorInput):
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
def handle_governor_params(payload: GovernorParamsInput):
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


# 5. DEBUG LOGS
@app.get("/log")
def get_full_app_state():
    try:
        return {"status": "success", "app_state": asdict(app_state)}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to dump application state state: {str(e)}",
        )


#  ==== THREAD & CORE ====


class ThreadInput(BaseModel):
    num_threads: int = 4


class CoreInput(BaseModel):
    cores: list[int] = [0, 1, 2, 3]


@app.get("/api/thread")
async def get_thread_state():
    try:
        return {"status": "success", "num_threads": app_state.model.num_threads}
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch thread allocation: {str(e)}"
        )


@app.post("/api/thread")
async def handle_thread_state(config: ThreadInput):
    try:
        if app_state.model.num_threads != config.num_threads:
            app_state.model.num_threads = config.num_threads
            app_state.model.reload_model_event.set()
            return {"status": "success", "num_threads": app_state.model.num_threads}
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to update thread allocation: {str(e)}"
        )


@app.get("/api/cores")
async def get_core_state():
    try:
        return {"status": "success", "cores": app_state.model.cores}
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch cores allocation: {str(e)}"
        )


@app.post("/api/cores")
async def handle_core_state(config: CoreInput):
    try:
        if app_state.model.cores != config.cores:
            app_state.model.cores = config.cores
            success = cpu_controller.apply_cores(config.cores)
            if not success:
                raise HTTPException(
                    status_code=500, detail="Failed to apply core affinity at OS level."
                )
            return {"status": "success", "cores": app_state.model.cores}
        return {"status": "success", "cores": app_state.model.cores}
    except HTTPException as http_err:
        raise http_err
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update cores allocation: {str(e)}")


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


@router.websocket("/ws/status")
async def cpu_status_websocket(websocket: WebSocket):
    await websocket.accept(headers=[(b"access-control-allow-origin", b"*")])
    try:
        while True:
            data = cpu_controller.get_cpu_status()
            await websocket.send_json(data)
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        print("Client disconnected from status websocket")


app.include_router(router)
