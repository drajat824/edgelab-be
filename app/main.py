from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import os
from dataclasses import dataclass, field, asdict
from typing import Any

# Import penampung state internal dan controller
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


class GovernorInput(BaseModel):
    governor: str

# GET GOVERNOR STATUS
@app.get("/api/cpu/governor")
def get_current_hardware_status():
    try:
        gov_path = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
        if os.path.exists(gov_path):
            with open(gov_path, "r") as f:
                governor = f.read().strip()
        else:
            governor = app_state.cpu.governor

        hardware_data = cpu_controller.get_governor_state(governor)
        app_state.cpu.governor = governor
        sub_state = getattr(app_state.cpu, governor, None)
        if sub_state:
            for key, val in hardware_data.items():
                if hasattr(sub_state, key):
                    setattr(sub_state, key, val)

        return {"status": "success", "governor": governor, governor: hardware_data}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# UPDATE GOVERNOR STATUS
@app.post("/api/cpu/governor")
def handle_governor_state(payload: GovernorInput):
    try:
        governor = payload.governor
        app_state.cpu.governor = governor
        current_freq = cpu_controller.apply_cpu_governor()
        if current_freq is None:
            raise Exception(
                "Gagal membaca atau menerapkan konfigurasi ke hardware Linux"
            )

        hardware_data = cpu_controller.get_governor_state(governor)
        sub_state = getattr(app_state.cpu, governor, None)
        if sub_state:
            for key, val in hardware_data.items():
                if hasattr(sub_state, key):
                    setattr(sub_state, key, val)

        return {"status": "success", "governor": governor, governor: hardware_data}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# CEK LOG
@app.get("/api/state")
def get_full_app_state():
    """
    Endpoint khusus untuk debugging/mengecek seluruh isi app_state saat ini.
    """
    try:
        return {"status": "success", "app_state": asdict(app_state)}
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Gagal melakukan dump state: {str(e)}"
        )
