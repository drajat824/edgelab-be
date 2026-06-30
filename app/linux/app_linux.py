import os
import time
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

        # Menggunakan sudo tee via subprocess agar aman berjalan di background tanpa prompt password
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

    def read_freq_file(self, filename: str) -> float | None:
        path = f"{self.SYS_CPU_BASE}/cpu0/cpufreq/{filename}"
        try:
            with open(path, "r") as f:
                freq_khz = int(f.read().strip())
                return freq_khz / 1000000
        except Exception as e:
            logging.error(f"Error saat membaca {path}: {e}")
            return None

    def apply_governor_tunables(self, gov_name: str, sub_state):
        # Pemetaan dari properti Dataclass kamu ke file asli di kernel Linux
        translation_map = {
            "thresholdUp": "up_threshold",
            "thresholdDown": "down_threshold",
            "samplingRate": "sampling_rate",
            "samplingDownFactor": "sampling_down_factor",
            "frequencyStep": "freq_step",
            "rateLimit": "rate_limit_us",
            "powerBias": "powersave_bias",
            "isIgnoreNice": "ignore_nice_load",
            "isIoBusy": "io_is_busy",
        }

        tunables_dir = f"{self.SYS_CPU_BASE}/cpufreq/{gov_name}"

        # PENTING: Beri waktu jeda (max 500ms) agar kernel Linux sempat membuat direktori tunables
        for _ in range(5):
            if os.path.exists(tunables_dir):
                break
            time.sleep(0.1)
        else:
            logging.warning(
                f"Direktori tunables tidak tersedia untuk governor: {gov_name}"
            )
            return

        # Ambil data dari dataclass menjadi dictionary Python
        params = asdict(sub_state)

        for key, val in params.items():
            if key in translation_map:
                # Jangan kirim field 'maxFreq' atau 'minFreq' ke fungsi ini jika ikut terbawa di dataclass
                if key in ["maxFreq", "minFreq"]:
                    continue

                linux_file = translation_map[key]
                path = f"{tunables_dir}/{linux_file}"

                # Konversi data boolean (True/False) menjadi (1/0) khas Linux
                if isinstance(val, bool):
                    val = 1 if val else 0

                # Hanya lewati jika nilainya murni None (bukan 0 atau False)
                if val is None:
                    continue

                self.write_sys_file(path, str(val))


    def apply_cpu_governor(self) -> dict | None:
        cpu = app_state.cpu
        gov_name = cpu.governor
        cmd = (
            f"echo {gov_name} | sudo tee {self.SYS_CPU_BASE}/cpu*/cpufreq/scaling_governor"
        )

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
            print(f"✓ Governor successfully changed to {gov_name.upper()}!")

            # 2. Terapkan parameter tunables & limit frekuensi (jika ada)
            if hasattr(cpu, gov_name):
                sub_state = getattr(cpu, gov_name)
                self.apply_governor_tunables(gov_name, sub_state)

                if hasattr(sub_state, "maxFreq") and getattr(sub_state, "maxFreq") > 0:
                    max_khz = int(sub_state.maxFreq * 1000000)
                    self.write_sys_file(
                        f"{self.SYS_CPU_BASE}/cpu*/cpufreq/scaling_max_freq", str(max_khz)
                    )

                if hasattr(sub_state, "minFreq") and getattr(sub_state, "minFreq") > 0:
                    min_khz = int(sub_state.minFreq * 1000000)
                    self.write_sys_file(
                        f"{self.SYS_CPU_BASE}/cpu*/cpufreq/scaling_min_freq", str(min_khz)
                    )

            # 3. REVISI: Baca kedua nilai sekaligus dari hardware Linux
            return {
                "max": self.read_freq_file("scaling_max_freq"),
                "min": self.read_freq_file("scaling_min_freq"),
            }

        except subprocess.CalledProcessError as e:
            logging.error(f"Gagal menerapkan governor. Error: {e.stderr.strip()}")
            return None
