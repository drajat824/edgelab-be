import os
import json
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from dataclasses import asdict

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


class GovernorParamsInput(BaseModel):
    performance: Optional[dict] = None
    powersave: Optional[dict] = None
    ondemand: Optional[dict] = None
    conservative: Optional[dict] = None
    schedutil: Optional[dict] = None
    userspace: Optional[dict] = None


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

        hardware_data = cpu_controller.get_governor_state()
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

        hardware_data = cpu_controller.get_governor_state()
        sub_state = getattr(app_state.cpu, governor, None)
        if sub_state:
            for key, val in hardware_data.items():
                if hasattr(sub_state, key):
                    setattr(sub_state, key, val)

        return {"status": "success", "governor": governor, governor: hardware_data}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# UPDATE GOVERNOR PARAMETER
@app.post("/api/cpu/governor/params")
def handle_governor_params(payload: GovernorParamsInput):
    try:
        governor = app_state.cpu.governor
        incoming_payload = getattr(payload, governor, None)
        incoming_params = {}

        if incoming_payload:
            if hasattr(incoming_payload, "model_dump"):
                incoming_params = incoming_payload.model_dump(exclude_unset=True)
            else:
                incoming_params = dict(incoming_payload)

        # Validasi: Jika payload kosong, kembalikan error 400
        if not incoming_params:
            raise HTTPException(
                status_code=400,
                detail=f"Gagal memproses params. Governor aktif saat ini adalah '{governor}', tetapi payload untuk '{governor}' kosong atau tidak dikirim.",
            )
        
        # Validasi: Pastikan semua key di incoming_params valid untuk governor yang aktif
        sub_state = getattr(app_state.cpu, governor, None)
        if sub_state:
            for key in incoming_params.keys():
                if not hasattr(sub_state, key):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Parameter '{key}' tidak valid untuk governor '{governor}'.",
                    )
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Governor '{governor}' tidak terdaftar di sistem aplikasi.",
            )

        # Terapkan perubahan ke sistem Linux
        cpu_controller.apply_governor_params(governor, incoming_params)

        # Simpan data yang dikirim ke app_state lokal
        sub_state = getattr(app_state.cpu, governor, None)
        if sub_state:
            for key, val in incoming_params.items():
                if hasattr(sub_state, key):
                    setattr(sub_state, key, val)

        # Ambil data real-time pasca-penulisan dari Linux Kernel
        hardware_data = cpu_controller.get_governor_state()
        return {"status": "success", "governor": governor, governor: hardware_data}

    # Tangkap HTTPException dari validasi di atas agar tidak berubah jadi error 500
    except HTTPException as http_err:
        raise http_err
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# CEK LOG
@app.get("/log")
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
