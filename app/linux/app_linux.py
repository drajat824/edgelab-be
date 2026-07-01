import os
import time
import glob
import logging
import subprocess
from dataclasses import asdict
from ..state.app_state import app_state

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


class LinuxCPUController:
    SYS_CPU_BASE = "/sys/devices/system/cpu"

    @staticmethod
    def write_sys_file(path: str, value: str):
        if not os.path.exists(path):
            logging.warning(
                f"Path tidak ditemukan (Mungkin kernel tidak mendukung): {path}"
            )
            return False

        # Menggunakan sudo tee via subprocess agar aman berjalan di background
        cmd = f"echo {value} | sudo tee {path}"
        try:
            subprocess.run(
                cmd,
                shell=True,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            return True
        except Exception as e:
            logging.error(f"Error saat menulis ke {path}: {e}")
            return False

    # Get Governor Status
    def get_governors(self) -> dict:
        governors = {}
        # Mencari semua file scaling_governor untuk setiap core
        files = glob.glob(f"{self.SYS_CPU_BASE}/cpu*/cpufreq/scaling_governor")
        
        for file_path in sorted(files):
            core_name = file_path.split('/')[-3] 
            try:
                with open(file_path, 'r') as f:
                    governors[core_name] = f.read().strip()
            except PermissionError:
                governors[core_name] = "Permission Denied"
            except Exception as e:
                governors[core_name] = f"Error: {e}"
                
        return governors
    
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
    def apply_cpu_governor(self) -> bool | None:
        cpu = app_state.cpu
        gov_name = cpu.governor
        cmd = f"echo {gov_name} | sudo tee {self.SYS_CPU_BASE}/cpu*/cpufreq/scaling_governor"

        try:
            # 1. Terapkan mode governor utama
            subprocess.run(
                cmd,
                shell=True,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
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
            if governor == "powersave" and "minFreq" in params and params["minFreq"] is not None:
                try:
                    freq_cmd = f"cat {self.SYS_CPU_BASE}/cpu0/cpufreq/scaling_available_frequencies"
                    freq_res = subprocess.run(freq_cmd, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    avail_frequencies = [int(f) for f in freq_res.stdout.split() if f.isdigit()]
                    
                    if avail_frequencies:
                        max_possible_raw = max(avail_frequencies)
                        max_cmd = f'for file in {self.SYS_CPU_BASE}/cpu*/cpufreq/scaling_max_freq; do echo {max_possible_raw} | sudo tee "$file"; done'
                        subprocess.run(max_cmd, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                except Exception as e:
                    print(f"[Warning] Gagal membuka jalur max_freq untuk powersave: {e}")
            
            # LOOP 1: Mengubah Frekuensi untuk SEMUA CORE (cpu*)
            for linux_file, dict_key in freq_map.items():
                if dict_key in params and params[dict_key] is not None:
                    raw_val = int(params[dict_key] * 1000000)
                    # FIX: Gunakan self.SYS_CPU_BASE/cpu* bukan base_dir
                    cmd = f'for file in {self.SYS_CPU_BASE}/cpu*/cpufreq/{linux_file}; do echo {raw_val} | sudo tee "$file"; done'
                    subprocess.run(cmd, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    
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
                        cmd = f'echo {raw_val} | sudo tee {file_path}'
                        subprocess.run(cmd, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            # Kondisi Khusus Userspace
            if governor == "userspace" and "fixedFrequency" in params and params["fixedFrequency"] is not None:
                raw_speed = int(params["fixedFrequency"] * 1000000)
                cmd = f'for file in {self.SYS_CPU_BASE}/cpu*/cpufreq/scaling_setspeed; do echo {raw_speed} | sudo tee "$file"; done'
                subprocess.run(cmd, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                
            return True

        except subprocess.CalledProcessError as e:
            logging.error(f"Gagal menulis parameter ke hardware. Error: {e.stderr.strip()}")
            return False