from dotenv import load_dotenv
import os
import logging
import subprocess
import psutil
from app_state import app_state
import json
import asyncio
import httpx
import logging

logger = logging.getLogger(__name__)

# Uniform logging format
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
load_dotenv()

SUDO_PASSWORD = os.getenv("SUDO_PASSWORD")
FREQ_RANGE = "[1.4, 1.7, 2.1]"
FREQ_GHZ = json.loads(FREQ_RANGE)


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
            logging.error(f"Error executing command: {cmd_string}")
            logging.error(f"Stderr: {e.stderr.strip()}")
            return ""

    # Get Governor Status
    def get_governors(self) -> dict:
        gov_file = f"{self.SYS_CPU_BASE}/cpu0/cpufreq/scaling_governor"

        if not os.path.exists(gov_file):
            logging.error(f"Governor file not found: {gov_file}")
            return {"cpu0": "Error: cpufreq driver not loaded or unsupported"}

        try:
            with open(gov_file, "r") as f:
                current_gov = f.read().strip()
            return {"cpu0": current_gov}
        except PermissionError:
            logging.error(f"Permission denied reading: {gov_file}")
            return {"cpu0": "Permission Denied"}
        except Exception as e:
            logging.error(f"Unexpected error reading governor: {e}")
            return {"cpu0": f"Error: {e}"}

    # Get Mode Governor & Global Frequencies
    def get_governor_state(self) -> dict:
        result = {}
        base_dir = f"{self.SYS_CPU_BASE}/cpu0/cpufreq"

        # 1. Fetch Global Frequencies
        freq_map = {"scaling_max_freq": "maxFreq", "scaling_min_freq": "minFreq"}
        for linux_file, dict_key in freq_map.items():
            file_path = f"{base_dir}/{linux_file}"
            if os.path.exists(file_path):
                try:
                    with open(file_path, "r") as f:
                        content = f.read().strip()
                        if content.isdigit():
                            result[dict_key] = int(content) / 1000000
                except (PermissionError, OSError) as e:
                    logging.warning(f"Failed to read frequency file {linux_file}: {e}")

        # 2. Fetch Active Governor Specific Tunables
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

                        if not raw_val:
                            continue

                        if linux_file.startswith(("ignore_", "io_")):
                            result[dict_key] = raw_val == "1"
                        elif "." in raw_val:
                            result[dict_key] = float(raw_val)
                        else:
                            if raw_val.isdigit() or (
                                raw_val.startswith("-") and raw_val[1:].isdigit()
                            ):
                                result[dict_key] = int(raw_val)
                    except (PermissionError, OSError) as e:
                        logging.warning(
                            f"Failed to read tunable file {linux_file}: {e}"
                        )

        # 3. Special Case for Userspace Governor
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
                except (PermissionError, OSError):
                    result["fixedFrequency"] = 0.0

            result["script"] = app_state.cpu.userspace.script
            result["isDynamicScripting"] = app_state.cpu.userspace.isDynamicScripting

        return result

    # Change Governor Mode
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
                f"Failed to apply governor: Governor name '{gov_name}' is invalid or unsupported by the system!"
            )
            return False  # Mengubah None menjadi False agar pengecekan di main.py lebih konsisten

        cmd = f'sudo sh -c \'for file in {self.SYS_CPU_BASE}/cpu*/cpufreq/scaling_governor; do echo "{gov_name}" > "$file"; done\''

        try:
            # Menggunakan skema deteksi berbasis output eksekusi command
            output = self.execute_cmd(cmd)
            # Jika eksekusi gagal (misal permision denied walau pakai sudo), biasanya cmd melempar log error internal
            logging.info(f"✓ Governor successfully changed to {gov_name.upper()}!")
            return True
        except Exception as e:
            logging.error(f"Failed to apply governor via shell. Error: {e}")
            return False

    # Change Governor Parameters & Global Frequencies
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

        # Jika direktori governor tidak ada di kernel kernel, langsung potong alur (fail-fast)
        if not os.path.exists(tunables_dir) and governor != "userspace":
            logging.error(f"Tunables directory does not exist for governor: {governor}")
            return False

        try:
            # LOOP: Internal Governor Tunables Change
            for linux_file, dict_key in tunables_map.items():
                if dict_key in params and params[dict_key] is not None:
                    val_from_frontend = params[dict_key]

                    if isinstance(val_from_frontend, bool):
                        raw_val = "1" if val_from_frontend else "0"
                    else:
                        raw_val = str(int(val_from_frontend))

                    file_path = f"{tunables_dir}/{linux_file}"
                    if os.path.exists(file_path):
                        cmd = f'sudo sh -c \'echo "{raw_val}" > "{file_path}"\''
                        self.execute_cmd(cmd)

            # Special Condition for Userspace
            if (
                governor == "userspace"
                and "fixedFrequency" in params
                and params["fixedFrequency"] is not None
            ):
                raw_speed = str(int(params["fixedFrequency"] * 1000000))
                cmd = f'sudo sh -c \'for file in {self.SYS_CPU_BASE}/cpu*/cpufreq/scaling_setspeed; do echo "{raw_speed}" > "$file"; done\''
                self.execute_cmd(cmd)

            return True

        except Exception as e:
            logging.error(
                f"Failed to write governor parameters to hardware. Error: {e}"
            )
            return False

    # Update Global Frequencies
    def apply_cpu_frequencies(
        self, min_freq: float | None, max_freq: float | None
    ) -> bool:
        try:
            target_min = min_freq if min_freq is not None else app_state.cpu.minFreq
            target_max = max_freq if max_freq is not None else app_state.cpu.maxFreq

            # Validation rules
            if target_min > target_max:
                logging.error(
                    f"Validation Error: minFreq ({target_min} GHz) cannot be greater than maxFreq ({target_max} GHz)!"
                )
                return False

            if min_freq is not None:
                raw_min = str(int(min_freq * 1000000))
                cmd_min = f'sudo sh -c \'for file in {self.SYS_CPU_BASE}/cpu*/cpufreq/scaling_min_freq; do echo "{raw_min}" > "$file"; done\''
                self.execute_cmd(cmd_min)

            if max_freq is not None:
                raw_max = str(int(max_freq * 1000000))
                cmd_max = f'sudo sh -c \'for file in {self.SYS_CPU_BASE}/cpu*/cpufreq/scaling_max_freq; do echo "{raw_max}" > "$file"; done\''
                self.execute_cmd(cmd_max)

            return True
        except Exception as e:
            logging.error(f"Failed to write CPU frequencies to hardware. Error: {e}")
            return False

    # ==== WEBSOCKET =====

    def get_cpu_utilization(self, max_cores: int = 4) -> dict:
        cores_usage = psutil.cpu_percent(interval=None, percpu=True)
        cores_usage = cores_usage[:max_cores]
        cores_usage = [round(x) for x in cores_usage]
        avg_usage = round(sum(cores_usage) / len(cores_usage)) if cores_usage else 0
        return {"average": avg_usage, "core": cores_usage}

    def get_cpu_status(self) -> dict:
        freq_cmd = "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"
        freq_raw = self.execute_cmd(freq_cmd)
        if freq_raw and freq_raw.isdigit():
            freq_ghz = f"{float(freq_raw) / 1000000:.1f} GHz"
        else:
            freq_ghz = "0.0 GHz"
        temp_cmd = "cat /sys/class/thermal/thermal_zone0/temp"
        temp_raw = self.execute_cmd(temp_cmd)

        if temp_raw and temp_raw.isdigit():
            temp_c = round(float(temp_raw) / 1000, 1)
        else:
            temp_c = 0.0

        app_state.cpu.userspaceMetrics.temp = temp_c
        return {"frequency": freq_ghz, "temperature": temp_c}


class MetricsFetcher:

    def __init__(
        self,
        state=app_state,
        url: str = "http://localhost:8041/api/userspace-metrics",
    ):
        self.app_state = state
        self.url = url
        self.running = False
        self._task = None
        self.linux_cpu_controller = LinuxCPUController()

        # State kontrol penahanan frekuensi
        self.is_held = False  # Menandai apakah frekuensi saat ini terkunci
        self._hold_requested_in_tick = (
            False  # Menandai apakah hold_frequency() dipanggil di tick ini
        )

    def start(self):
        if not self.running:
            self.running = True
            try:
                loop = asyncio.get_running_loop()
                self._task = loop.create_task(self._poll_loop())
            except RuntimeError:
                loop = asyncio.get_event_loop()
                self._task = asyncio.run_coroutine_threadsafe(self._poll_loop(), loop)

    def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()

    async def _poll_loop(self):
        async with httpx.AsyncClient(timeout=2) as client:
            while self.running:
                try:
                    response = await client.get(self.url)
                    if response.status_code == 200:
                        data = response.json()
                        fps_cam = float(data.get("fps_camera", 0.0))
                        fps_inf = float(data.get("inference_fps", 0.0))

                        metrics = self.app_state.cpu.userspaceMetrics
                        metrics.camera_fps = fps_cam
                        metrics.inference_fps = fps_inf
                        metrics.inference_running = fps_inf > 0
                    else:
                        self._set_fallback_state()

                except Exception:
                    self._set_fallback_state()

                await asyncio.sleep(0.5)

    def _set_fallback_state(self):
        metrics = self.app_state.cpu.userspaceMetrics
        metrics.camera_fps = 0.0
        metrics.inference_fps = 0.0
        metrics.inference_running = False

    # ==== HOOK UNTUK EXECUTOR / RUNNER SCRIPT ====

    def on_script_execution_start(self):
        self._hold_requested_in_tick = False

    def on_script_execution_end(self):
        if not self._hold_requested_in_tick:
            self.is_held = False

    # ==== USERSPACE - DYNAMIC SCRIPTING API ====

    def get_valid_freqs(self) -> list[float]:
        min_f = app_state.cpu.minFreq
        max_f = app_state.cpu.maxFreq
        valid_freqs = [f for f in FREQ_GHZ if min_f <= f <= max_f]
        return sorted(valid_freqs) if valid_freqs else sorted(FREQ_GHZ)

    def userspace_set_frequency(self, step: int) -> bool:
        if self.is_held:
            return False

        freqs = self.get_valid_freqs()
        curent_f = app_state.cpu.userspace.fixedFrequency
        current_idx = min(
            range(len(freqs)),
            key=lambda i: abs(freqs[i] - curent_f),
        )

        max_idx = len(freqs) - 1
        new_idx = max(0, min(max_idx, current_idx + step))
        target_freq = freqs[new_idx]

        app_state.cpu.userspace.fixedFrequency = target_freq
        return self.linux_cpu_controller.apply_governor_params(
            governor="userspace", params={"fixedFrequency": target_freq}
        )

    def userspace_hold_frequency(self) -> bool:
        self.is_held = True
        self._hold_requested_in_tick = True
        return True

    def userspace_set_min_frequency(self) -> bool:
        self.is_held = False
        freqs = self.get_valid_freqs()
        target_freq = freqs[0]
        app_state.cpu.userspace.fixedFrequency = target_freq
        return self.linux_cpu_controller.apply_governor_params(
            governor="userspace", params={"fixedFrequency": target_freq}
        )

    def userspace_set_max_frequency(self) -> bool:
        self.is_held = False
        freqs = self.get_valid_freqs()
        target_freq = freqs[-1]
        app_state.cpu.userspace.fixedFrequency = target_freq
        return self.linux_cpu_controller.apply_governor_params(
            governor="userspace", params={"fixedFrequency": target_freq}
        )


class DynamicScriptEngine:
    def __init__(self, app_state, cpu_controller):
        self.app_state = app_state
        self.cpu = cpu_controller
        self.running = False
        self._task = None
        self._current_script_str = ""
        self._compiled_code = None
        self.metrix_fetcher = MetricsFetcher()

    def start(self, interval: float = 1):
        if not self.running:
            self.running = True
            try:
                loop = asyncio.get_running_loop()
                self._task = loop.create_task(self._execution_loop(interval))
            except RuntimeError:
                loop = asyncio.get_event_loop()
                self._task = asyncio.run_coroutine_threadsafe(
                    self._execution_loop(interval), loop
                )
            logger.info("DynamicScriptEngine started.")

    def stop(self):
        self.running = False
        if self._task:
            if isinstance(self._task, asyncio.Task):
                self._task.cancel()
        logger.info("DynamicScriptEngine stopped.")

    def _sync_and_compile_script(self):
        userspace_state = getattr(self.app_state.cpu, "userspace", None)
        if not userspace_state:
            return None
        latest_script = userspace_state.script or ""

        if latest_script != self._current_script_str:
            self._current_script_str = latest_script
            if latest_script.strip():
                try:
                    self._compiled_code = compile(
                        latest_script, "<userspace_script>", "exec"
                    )
                    logger.info("Script compile success")
                except SyntaxError as e:
                    logger.error(f"Syntax Error: {e}")
                    self._compiled_code = None
            else:
                self._compiled_code = None

        return self._compiled_code

    def _build_context(self) -> dict:
        metrics = getattr(self.app_state.cpu, "userspaceMetrics", None)
        cam_fps = float(metrics.camera_fps) if metrics else 0.0
        inf_running = bool(metrics.inference_running) if metrics else False
        cpu_temp = float(getattr(metrics, "temp", 0.0))

        return {
            "cpu_temp": cpu_temp,
            "camera_fps": cam_fps,
            "inference_running": inf_running,
            "set_frequency": self.metrix_fetcher.userspace_set_frequency,
            "set_min_frequency": self.metrix_fetcher.userspace_set_min_frequency,
            "set_max_frequency": self.metrix_fetcher.userspace_set_max_frequency,
            "hold_frequency": self.metrix_fetcher.userspace_hold_frequency,
        }

    async def _execution_loop(self, interval: float):
        while self.running:
            try:
                userspace_state = getattr(self.app_state.cpu, "userspace", None)
                if userspace_state and userspace_state.isDynamicScripting:
                    compiled_code = self._sync_and_compile_script()
                    if compiled_code:
                        context = self._build_context()
                        self.metrix_fetcher.on_script_execution_start()
                        await asyncio.to_thread(exec, compiled_code, context)
                        self.metrix_fetcher.on_script_execution_end()

            except Exception as e:
                logger.error(f"[User Script Exec Error]: {e}")
            await asyncio.sleep(interval)
