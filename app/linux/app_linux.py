from dotenv import load_dotenv
import os
import logging
import subprocess
from ..state.app_state import app_state

# Mengatur format logging agar seragam
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
load_dotenv()

SUDO_PASSWORD = os.getenv("SUDO_PASSWORD")


class LinuxCPUController:
    SYS_CPU_BASE = "/sys/devices/system/cpu"

    def execute_cmd(self, cmd_string: str) -> str:
        if "sudo " in cmd_string and "-S" not in cmd_string:
            cmd_string = cmd_string.replace("sudo ", "sudo -S ")

        try:
            result = subprocess.run(
                cmd_string,
                shell=True,
                input=f"{SUDO_PASSWORD}\n" if SUDO_PASSWORD else None,
                text=True,
                capture_output=True,
                check=True,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            logging.error(f"Error saat mengeksekusi: {cmd_string}")
            logging.error(f"Stderr: {e.stderr.strip()}")
            return ""

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

    # Get Mode Governor & Global Frequencies
    def get_governor_state(self) -> dict:
        result = {}
        base_dir = f"{self.SYS_CPU_BASE}/cpu0/cpufreq"

        # 1. Ambil Frekuensi Global (Sekarang dibaca di governor apapun)
        freq_map = {"scaling_max_freq": "maxFreq", "scaling_min_freq": "minFreq"}
        for linux_file, dict_key in freq_map.items():
            file_path = f"{base_dir}/{linux_file}"
            if os.path.exists(file_path):
                try:
                    with open(file_path, "r") as f:
                        result[dict_key] = int(f.read().strip()) / 1000000
                except Exception:
                    pass

        # 2. Ambil Parameter Tunables Spesifik Governor yang Aktif
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
                        result["fixedFrequency"] = int(raw_speed) / 1000000
                    else:
                        result["fixedFrequency"] = 0.0
                except Exception:
                    pass

        return result

    # Ganti Mode Governor
    def apply_cpu_governor(self, gov_name: str) -> bool | None:
        ALLOWED_GOVERNORS = [
            "conservative",
            "ondemand",
            "userspace",
            "powersave",
            "performance",
            "schedutil",
        ]

        if gov_name not in ALLOWED_GOVERNORS:
            logging.error(
                f"Gagal menerapkan governor: Nama governor '{gov_name}' tidak valid/tidak didukung oleh sistem!"
            )
            return None

        cmd = f'sudo sh -c \'for file in {self.SYS_CPU_BASE}/cpu*/cpufreq/scaling_governor; do echo "{gov_name}" > "$file"; done\''

        try:
            self.execute_cmd(cmd)
            logging.info(f"✓ Governor successfully changed to {gov_name.upper()}!")
            return True

        except Exception as e:
            logging.error(f"Gagal menerapkan governor. Error: {e}")
            return None

    # Ganti Parameter Governor & Global Frequencies
    def apply_governor_params(self, governor: str, params: dict) -> bool:
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
            # LOOP: Mengubah Tunables Governor Internal
            for linux_file, dict_key in tunables_map.items():
                if dict_key in params and params[dict_key] is not None:
                    val_from_frontend = params[dict_key]

                    if isinstance(val_from_frontend, bool):
                        raw_val = "1" if val_from_frontend else "0"
                    else:
                        raw_val = int(val_from_frontend)

                    file_path = f"{tunables_dir}/{linux_file}"
                    if os.path.exists(file_path):
                        cmd = f'sudo sh -c \'echo "{raw_val}" > "{file_path}"\''
                        self.execute_cmd(cmd)

            # Kondisi Khusus Userspace (tetap di sini karena bersifat tuning parameter)
            if (
                governor == "userspace"
                and "fixedFrequency" in params
                and params["fixedFrequency"] is not None
            ):
                raw_speed = int(params["fixedFrequency"] * 1000000)
                cmd = f'sudo sh -c \'for file in {self.SYS_CPU_BASE}/cpu*/cpufreq/scaling_setspeed; do echo "{raw_speed}" > "$file"; done\''
                self.execute_cmd(cmd)

            return True

        except Exception as e:
            logging.error(f"Gagal menulis parameter governor ke hardware. Error: {e}")
            return False

    # FUNGSI BARU: KHUSUS UNTUK UPDATE FREKUENSI GLOBAL
    def apply_cpu_frequencies(
        self, min_freq: float | None, max_freq: float | None
    ) -> bool:
        try:
            # Mengambil data pembanding dari state jika salah satu parameter tidak dikirim
            target_min = min_freq if min_freq is not None else app_state.cpu.minFreq
            target_max = max_freq if max_freq is not None else app_state.cpu.maxFreq

            # Validasi aturan dasar hardware
            if target_min > target_max:
                logging.error(
                    f"Validation Error: minFreq ({target_min} GHz) tidak boleh lebih besar dari maxFreq ({target_max} GHz)!"
                )
                return False

            # Tulis minFreq ke sistem jika ada di payload
            if min_freq is not None:
                raw_min = int(min_freq * 1000000)
                cmd_min = f'sudo sh -c \'for file in {self.SYS_CPU_BASE}/cpu*/cpufreq/scaling_min_freq; do echo "{raw_min}" > "$file"; done\''
                self.execute_cmd(cmd_min)

            # Tulis maxFreq ke sistem jika ada di payload
            if max_freq is not None:
                raw_max = int(max_freq * 1000000)
                cmd_max = f'sudo sh -c \'for file in {self.SYS_CPU_BASE}/cpu*/cpufreq/scaling_max_freq; do echo "{raw_max}" > "$file"; done\''
                self.execute_cmd(cmd_max)

            return True
        except Exception as e:
            logging.error(f"Gagal menulis frekuensi ke hardware. Error: {e}")
            return False
