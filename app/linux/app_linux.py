import os
import time
import glob
import logging
import subprocess
from dataclasses import asdict
from ..state.app_state import app_state
from functools import wraps
from typing import ParamSpec

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

P = ParamSpec("P")
SUDO_PASSWORD = os.getenv("batman123")

class LinuxCPUController:
    SYS_CPU_BASE = "/sys/devices/system/cpu"

    def execute_cmd(self, cmd: str) -> str:
        is_sudo = cmd.strip().startswith("sudo")
        input_data = f"{SUDO_PASSWORD}\n" if is_sudo else None

        try:
            result = subprocess.run(
                cmd,
                shell=True,
                check=True,
                input=input_data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,  # Memastikan semua output otomatis dikonversi dari bytes ke string murni
            )
            return result.stdout.strip()

        except subprocess.CalledProcessError as e:
            # Mengembalikan stderr dari sistem Linux agar debugging mudah jika command gagal
            raise RuntimeError(
                f"Gagal mengeksekusi command: {cmd}. Error: {e.stderr.strip()}"
            )

    # Get Governor Status
    def get_governors(self) -> dict:
        gov_file = f"{self.SYS_CPU_BASE}/cpu0/cpufreq/scaling_governor"
        
        try:
            with open(gov_file, "r") as f:
                current_gov = f.read().strip()
            return {"cpu0": current_gov}
        except PermissionError:
            return {"cpu0": "Permission Denied"}
        except Exception as e:
            return {"cpu0": f"Error: {e}"}

    # Get Mode Governor
    def get_governor_state(self) -> dict:
        result = {}
        base_dir = f"{self.SYS_CPU_BASE}/cpu0/cpufreq"
        freq_map = {"scaling_max_freq": "maxFreq", "scaling_min_freq": "minFreq"}
        governors_dict = self.get_governors()
        governor = governors_dict.get("cpu0", "powersave")

        tunables_dir = f"{self.SYS_CPU_BASE}/cpufreq/{governor}"
        tunables_map = {
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

        for linux_file, dict_key in freq_map.items():
            if governor == "performance" and dict_key == "minFreq":
                continue
            if governor == "powersave" and dict_key == "maxFreq":
                continue

            file_path = f"{base_dir}/{linux_file}"
            if os.path.exists(file_path):
                try:
                    with open(file_path, "r") as f:
                        result[dict_key] = int(f.read().strip()) / 1000000
                except Exception:
                    pass

        if os.path.exists(tunables_dir):
            for linux_file, dict_key in tunables_map.items():
                file_path = f"{tunables_dir}/{linux_file}"
                if os.path.exists(file_path):
                    try:
                        with open(file_path, "r") as f:
                            raw_val = f.read().strip()

                        if linux_file.startswith(("ignore_", "io_")):
                            result[dict_key] = raw_val == "1"
                        elif "." in raw_val:
                            result[dict_key] = float(raw_val)
                        else:
                            result[dict_key] = int(raw_val)
                    except Exception:
                        continue

        # 3. Kasus Khusus untuk Governor Userspace
        if governor == "userspace":
            setspeed_file = f"{base_dir}/scaling_setspeed"
            if os.path.exists(setspeed_file):
                try:
                    with open(setspeed_file, "r") as f:
                        raw_speed = f.read().strip()
                    if "unsupported" not in raw_speed and raw_speed.isdigit():
                        result["fixFreq"] = int(raw_speed) / 1000000
                    else:
                        result["fixFreq"] = 0.0
                except Exception:
                    pass

        return result

    # Ganti Mode Governor
    def apply_cpu_governor(self, gov_name: str) -> bool | None:
        cmd = f"echo {gov_name} | sudo tee {self.SYS_CPU_BASE}/cpu*/cpufreq/scaling_governor"
        ALLOWED_GOVERNORS = [
            "conservative", 
            "ondemand", 
            "userspace", 
            "powersave", 
            "performance", 
            "schedutil"
        ]
        
        if gov_name not in ALLOWED_GOVERNORS:
            logging.error(
                f"Gagal menerapkan governor: Nama governor '{gov_name}' tidak valid/tidak didukung oleh sistem!"
            )
            return None

        try:
            self.execute_cmd(cmd)
            logging.info(f"✓ Governor successfully changed to {gov_name.upper()}!")
            return True

        except subprocess.CalledProcessError as e:
            logging.error(f"Gagal menerapkan governor. Error: {e.stderr.strip()}")
            return None

    # Ganti Parameter Governor
    def apply_governor_params(self, governor: str, params: dict) -> bool:
        freq_map = {"scaling_max_freq": "maxFreq", "scaling_min_freq": "minFreq"}
        tunables_dir = f"{self.SYS_CPU_BASE}/cpufreq/{governor}"
        tunables_map = {
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

        try:

            # Menaikan max_freq ke nilai maksimum yang tersedia jika governor adalah powersave dan minFreq diatur
            if (
                governor == "powersave"
                and "minFreq" in params
                and params["minFreq"] is not None
            ):
                try:
                    freq_cmd = f"cat {self.SYS_CPU_BASE}/cpu0/cpufreq/scaling_available_frequencies"
                    freq_res = self.execute_cmd(freq_cmd)
                    avail_frequencies = [
                        int(f) for f in freq_res.split() if f.isdigit()
                    ]
                    if avail_frequencies:
                        max_possible_raw = max(avail_frequencies)
                        max_cmd = f'for file in {self.SYS_CPU_BASE}/cpu*/cpufreq/scaling_max_freq; do echo {max_possible_raw} | sudo tee "$file"; done'
                        self.execute_cmd(max_cmd)
                except Exception as e:
                    print(
                        f"[Warning] Gagal membuka jalur max_freq untuk powersave: {e}"
                    )

            # LOOP 1: Mengubah Frekuensi untuk SEMUA CORE (cpu*)
            for linux_file, dict_key in freq_map.items():
                if dict_key in params and params[dict_key] is not None:
                    raw_val = int(params[dict_key] * 1000000)
                    cmd = f'for file in {self.SYS_CPU_BASE}/cpu*/cpufreq/{linux_file}; do echo {raw_val} | sudo tee "$file"; done'
                    self.execute_cmd(cmd)

            # LOOP 2: Mengubah Tunables Governor Internal
            for linux_file, dict_key in tunables_map.items():
                if dict_key in params and params[dict_key] is not None:
                    val_from_frontend = params[dict_key]

                    if isinstance(val_from_frontend, bool):
                        raw_val = "1" if val_from_frontend else "0"
                    else:
                        raw_val = int(val_from_frontend)

                    file_path = f"{tunables_dir}/{linux_file}"
                    if os.path.exists(file_path):
                        cmd = f"echo {raw_val} | sudo tee {file_path}"
                        self.execute_cmd(cmd)

            # Kondisi Khusus Userspace
            if (
                governor == "userspace"
                and "fixFreq" in params
                and params["fixFreq"] is not None
            ):
                raw_speed = int(params["fixFreq"] * 1000000)
                cmd = f'for file in {self.SYS_CPU_BASE}/cpu*/cpufreq/scaling_setspeed; do echo {raw_speed} | sudo tee "$file"; done'
                self.execute_cmd(cmd)

            return True

        except subprocess.CalledProcessError as e:
            logging.error(
                f"Gagal menulis parameter ke hardware. Error: {e.stderr.strip()}"
            )
            return False
