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

# --- PYDANTIC SCHEMAS ---


class GovernorInput(BaseModel):
    governor: str


class FrequencyInput(BaseModel):
    minFreq: Optional[float] = None
    maxFreq: Optional[float] = None


class GovernorParamsInput(BaseModel):
    # Parameter dibuat flat (tidak nested), otomatis disesuaikan dengan governor aktif
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

        if "Error" in governor or "Permission" in governor:
            governor = app_state.cpu.governor
        else:
            app_state.cpu.governor = governor

        hardware_data = cpu_controller.get_governor_state()

        # Sinkronisasi ke Global State CPU
        if "minFreq" in hardware_data:
            app_state.cpu.minFreq = hardware_data["minFreq"]
        if "maxFreq" in hardware_data:
            app_state.cpu.maxFreq = hardware_data["maxFreq"]

        # Sinkronisasi ke Sub-State Governor yang Aktif
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
        raise HTTPException(status_code=500, detail=str(e))


# 2. UPDATE GOVERNOR SELECTION
@app.post("/api/cpu/governor")
def handle_governor_state(payload: GovernorInput):
    try:
        governor = payload.governor
        success = cpu_controller.apply_cpu_governor(governor)
        if not success:
            raise Exception("Gagal menerapkan konfigurasi governor ke hardware Linux")

        app_state.cpu.governor = governor
        return get_current_hardware_status()

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# 3. ENDPOINT BARU: UPDATE GLOBAL FREQUENCIES
@app.post("/api/cpu/frequency")
def handle_cpu_frequency(payload: FrequencyInput):
    try:
        if payload.minFreq is None and payload.maxFreq is None:
            raise HTTPException(
                status_code=400,
                detail="Harus mengirimkan setidaknya minFreq atau maxFreq.",
            )

        # Terapkan langsung ke hardware via controller khusus frekuensi
        success = cpu_controller.apply_cpu_frequencies(payload.minFreq, payload.maxFreq)
        if not success:
            raise HTTPException(
                status_code=400,
                detail="Gagal memperbarui frekuensi. Pastikan minFreq tidak > maxFreq.",
            )

        # Jika sukses, update state lokal aplikasi
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
        raise HTTPException(status_code=500, detail=str(e))


# 4. UPDATE GOVERNOR TUNABLES (MURNI TUNING)
@app.post("/api/cpu/governor/params")
def handle_governor_params(payload: GovernorParamsInput):
    try:
        governor = app_state.cpu.governor
        sub_state = getattr(app_state.cpu, governor, None)

        if not sub_state:
            raise HTTPException(
                status_code=400,
                detail=f"Governor '{governor}' tidak memiliki parameter tunables.",
            )

        # Ambil data input yang tidak bernilai None
        incoming_params = payload.model_dump(exclude_unset=True)
        if not incoming_params:
            raise HTTPException(status_code=400, detail="Payload parameter kosong.")

        # Validasi kecocokan parameter dengan governor aktif
        for key in incoming_params.keys():
            if not hasattr(sub_state, key):
                raise HTTPException(
                    status_code=400,
                    detail=f"Parameter '{key}' tidak valid untuk governor '{governor}'.",
                )

        # Terapkan ke hardware
        success = cpu_controller.apply_governor_params(governor, incoming_params)
        if not success:
            raise HTTPException(
                status_code=500, detail="Gagal menulis parameter internal ke kernel."
            )

        # Simpan ke local state
        for key, val in incoming_params.items():
            setattr(sub_state, key, val)

        return get_current_hardware_status()

    except HTTPException as http_err:
        raise http_err
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# 5. DEBUG LOGS
@app.get("/log")
def get_full_app_state():
    try:
        return {"status": "success", "app_state": asdict(app_state)}
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Gagal melakukan dump state: {str(e)}"
        )
