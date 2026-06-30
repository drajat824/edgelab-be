from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dataclasses import asdict
import os
from .state.app_state import app_state, CPUState
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


def sync_hardware_to_state(gov_name: str, freqs: dict):
    if not hasattr(app_state.cpu, gov_name):
        return

    # 1. Inisialisasi sub_state terlebih dahulu agar aman digunakan ke bawah
    sub_state = getattr(app_state.cpu, gov_name)

    # 2. Sinkronisasi Frekuensi Global (Max & Min)
    if hasattr(sub_state, "maxFreq") and freqs.get("max") is not None:
        sub_state.maxFreq = freqs["max"]
    if hasattr(sub_state, "minFreq") and freqs.get("min") is not None:
        sub_state.minFreq = freqs["min"]

    # 3. Sinkronisasi Parameter Tunables Lainnya dari Hardware
    # Pemetaan terbalik: dari nama file di Linux ke properti Dataclass kamu
    reverse_translation_map = {
        "up_threshold": "thresholdUp",
        "down_threshold": "thresholdDown",
        "sampling_rate": "samplingRate",
        "sampling_down_factor": "samplingDownFactor",
        "freq_step": "frequencyStep",
        "rate_limit_us": "rateLimit",
        "powersave_bias": "powerBias",
        "ignore_nice_load": "isIgnoreNice",
        "io_is_busy": "isIoBusy",
    }

    tunables_dir = f"/sys/devices/system/cpu/cpufreq/{gov_name}"

    # Jika foldernya ada di Linux, baca isinya satu per satu
    if os.path.exists(tunables_dir):
        for linux_file, dataclass_key in reverse_translation_map.items():
            if hasattr(sub_state, dataclass_key):
                file_path = f"{tunables_dir}/{linux_file}"
                if os.path.exists(file_path):
                    try:
                        with open(file_path, "r") as f:
                            raw_val = f.read().strip()

                        current_val = getattr(sub_state, dataclass_key)

                        if isinstance(current_val, bool):
                            setattr(sub_state, dataclass_key, raw_val == "1")
                        elif isinstance(current_val, int):
                            setattr(sub_state, dataclass_key, int(raw_val))
                        elif isinstance(current_val, float):
                            setattr(sub_state, dataclass_key, float(raw_val))
                    except Exception:
                        continue

    # 4. REVISI POSISI: Kasus Khusus Userspace diletakkan di bawah setelah sub_state siap
    if gov_name == "userspace" and hasattr(sub_state, "fixedFrequency"):
        setspeed_file = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_setspeed"

        if os.path.exists(setspeed_file):
            try:
                with open(setspeed_file, "r") as f:
                    raw_speed = f.read().strip()

                if "unsupported" in raw_speed or not raw_speed.isdigit():
                    sub_state.fixedFrequency = 0.0
                else:
                    # Konversi kembali dari KHz ke GHz (misal: 1500000 KHz -> 1.5 GHz)
                    sub_state.fixedFrequency = int(raw_speed) / 1000000
            except Exception:
                pass


@app.get("/api/cpu")
def get_cpu_status():
    current_freq = cpu_controller.apply_cpu_governor()
    active_governor = app_state.cpu.governor

    if current_freq is not None:
        sync_hardware_to_state(active_governor, current_freq)

    return {"status": "success", "cpu": asdict(app_state.cpu)}


@app.post("/api/cpu/governor")
def handle_cpu_update(new_settings: CPUState):
    try:
        app_state.cpu = new_settings
        active_governor = app_state.cpu.governor

        current_freq = cpu_controller.apply_cpu_governor()
        if current_freq is None:
            raise Exception(
                "Gagal membaca atau menerapkan konfigurasi ke hardware Linux"
            )

        sync_hardware_to_state(active_governor, current_freq)

        return {
            "status": "success",
            "message": f"Governor {active_governor.upper()} beserta seluruh parameter berhasil diterapkan!",
            "cpu": asdict(app_state.cpu),
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
